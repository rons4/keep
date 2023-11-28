import base64
import json
import logging
import os
import shutil
import subprocess
import zlib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pusher import Pusher
from sqlmodel import Session

from keep.api.core.db import enrich_alert as enrich_alert_db
from keep.api.core.db import get_alerts as get_alerts_from_db
from keep.api.core.db import get_enrichments as get_enrichments_from_db
from keep.api.core.db import get_session
from keep.api.core.dependencies import (
    get_pusher_client,
    verify_api_key,
    verify_bearer_token,
    verify_token_or_key,
)
from keep.api.models.alert import AlertDto, DeleteRequestBody, EnrichAlertRequestBody
from keep.api.models.db.alert import Alert
from keep.contextmanager.contextmanager import ContextManager
from keep.providers.providers_factory import ProvidersFactory
from keep.workflowmanager.workflowmanager import WorkflowManager

router = APIRouter()
logger = logging.getLogger(__name__)


def __send_compressed_alerts(
    compressed_alerts_data: str,
    number_of_alerts_in_batch: int,
    tenant_id: str,
    pusher_client: Pusher,
):
    """
    Sends a batch of pulled alerts via pusher.

    Args:
        compressed_alerts_data (str): The compressed data to send.
        number_of_alerts_in_batch (int): The number of alerts in the batch.
        tenant_id (str): The tenant id.
        pusher_client (Pusher): The pusher client.
    """
    logger.info(
        f"Sending batch of pulled alerts via pusher (alerts: {number_of_alerts_in_batch})",
        extra={
            "number_of_alerts": number_of_alerts_in_batch,
        },
    )
    pusher_client.trigger(
        f"private-{tenant_id}",
        "async-alerts",
        compressed_alerts_data,
    )


def _get_alerts_from_providers(tenant_id: str, pusher_client: Pusher = None):
    # TODO: this should be done in a more elegant way
    all_providers = ProvidersFactory.get_all_providers()
    all_alerts = []
    context_manager = ContextManager(
        tenant_id=tenant_id,
        workflow_id=None,
    )
    installed_providers = ProvidersFactory.get_installed_providers(
        tenant_id=tenant_id, all_providers=all_providers
    )
    logger.info("Asyncronusly pulling alerts from installed providers")
    for provider in installed_providers:
        provider_class = ProvidersFactory.get_provider(
            context_manager=context_manager,
            provider_id=provider.id,
            provider_type=provider.type,
            provider_config=provider.details,
        )
        try:
            logger.info(
                f"Pulling alerts from provider {provider.type} ({provider.id})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                },
            )
            alerts = provider_class.get_alerts()
            logger.info(
                f"Pulled alerts from provider {provider.type} ({provider.id})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                    "num_of_alerts": len(alerts),
                },
            )

            if alerts:
                # enrich also the pulled alerts:
                pulled_alerts_fingerprints = list(
                    set([alert.fingerprint for alert in alerts])
                )
                pulled_alerts_enrichments = get_enrichments_from_db(
                    tenant_id=tenant_id, fingerprints=pulled_alerts_fingerprints
                )
                logger.info(
                    "Enriching pulled alerts",
                    extra={
                        "provider_type": provider.type,
                        "provider_id": provider.id,
                        "tenant_id": tenant_id,
                    },
                )
                for alert_enrichment in pulled_alerts_enrichments:
                    for alert in alerts:
                        if alert_enrichment.alert_fingerprint == alert.fingerprint:
                            # enrich
                            for enrichment in alert_enrichment.enrichments:
                                # set the enrichment
                                setattr(
                                    alert,
                                    enrichment,
                                    alert_enrichment.enrichments[enrichment],
                                )
                logger.info(
                    "Enriched pulled alerts",
                    extra={
                        "provider_type": provider.type,
                        "provider_id": provider.id,
                        "tenant_id": tenant_id,
                    },
                )
                if pusher_client:
                    logger.info("Batch sending pulled alerts via pusher")
                    batch_send = []
                    previous_compressed_batch = ""
                    new_compressed_batch = ""
                    number_of_alerts_in_batch = 0
                    # tb: this might be too slow in the future and we might need to refactor
                    for alert in alerts:
                        alert_dict = alert.dict()
                        batch_send.append(alert_dict)
                        new_compressed_batch = base64.b64encode(
                            zlib.compress(json.dumps(batch_send).encode(), level=9)
                        ).decode()
                        if len(new_compressed_batch) <= 10240:
                            number_of_alerts_in_batch += 1
                            previous_compressed_batch = new_compressed_batch
                        else:
                            __send_compressed_alerts(
                                previous_compressed_batch,
                                number_of_alerts_in_batch,
                                tenant_id,
                                pusher_client,
                            )
                            batch_send = [alert_dict]
                            new_compressed_batch = ""
                            number_of_alerts_in_batch = 1

                    # this means we didn't get to this ^ else statement and loop ended
                    #   so we need to send the rest of the alerts
                    if new_compressed_batch and len(new_compressed_batch) < 10240:
                        __send_compressed_alerts(
                            new_compressed_batch,
                            number_of_alerts_in_batch,
                            tenant_id,
                            pusher_client,
                        )
                    logger.info("Sent batch of pulled alerts via pusher")
                # backward compatibility for CLI
                else:
                    all_alerts.extend(alerts)
            logger.info(
                f"Pulled alerts from provider {provider.type} ({provider.id}) (alerts: {len(alerts)})",
                extra={
                    "provider_type": provider.type,
                    "provider_id": provider.id,
                    "tenant_id": tenant_id,
                },
            )
        except Exception as e:
            logger.warn(
                f"Could not fetch alerts from provider due to {e}",
                extra={
                    "provider_id": provider.id,
                    "provider_type": provider.type,
                    "tenant_id": tenant_id,
                },
            )
            pass
    if not pusher_client:
        return all_alerts
    pusher_client.trigger(f"private-{tenant_id}", "async-done", {})
    logger.info("Asyncronusly fetched alerts from installed providers")


def _get_alerts_from_db(tenant_id):
    db_alerts = get_alerts_from_db(tenant_id=tenant_id)
    # enrich the alerts with the enrichment data
    for alert in db_alerts:
        if alert.alert_enrichment:
            alert.event.update(alert.alert_enrichment.enrichments)
    alerts = [AlertDto(**alert.event) for alert in db_alerts]
    return alerts


@router.get(
    "",
    description="Get alerts",
)
def get_all_alerts(
    background_tasks: BackgroundTasks,
    tenant_id: str = Depends(verify_token_or_key),
    pusher_client: Pusher = Depends(get_pusher_client),
) -> list[AlertDto]:
    logger.info(
        "Fetching alerts from DB",
        extra={
            "tenant_id": tenant_id,
        },
    )
    alerts = _get_alerts_from_db(tenant_id)
    logger.info(
        "Fetched alerts from DB",
        extra={
            "tenant_id": tenant_id,
        },
    )
    logger.info("Adding task to fetch async alerts from providers")
    background_tasks.add_task(_get_alerts_from_providers, tenant_id, pusher_client)
    logger.info("Added task to async fetch alerts from providers")
    return alerts


@router.delete("", description="Delete alert by name")
def delete_alert(
    delete_alert: DeleteRequestBody,
    tenant_id: str = Depends(verify_bearer_token),
) -> dict[str, str]:
    logger.info(
        "Deleting alert",
        extra={
            "fingerprint": delete_alert.fingerprint,
            "restore": delete_alert.restore,
            "tenant_id": tenant_id,
        },
    )

    enrich_alert_db(
        tenant_id=tenant_id,
        fingerprint=delete_alert.fingerprint,
        enrichments={"deleted": not delete_alert.restore},
    )

    logger.info(
        "Deleted alert successfully",
        extra={
            "tenant_id": tenant_id,
            "restore": delete_alert.restore,
            "fingerprint": delete_alert.fingerprint,
        },
    )
    return {"status": "ok"}


def handle_formatted_events(
    tenant_id,
    provider_type,
    session: Session,
    formatted_events: list[AlertDto],
    pusher_client: Pusher,
    provider_id: str | None = None,
):
    logger.info(
        "Asyncronusly adding new alerts to the DB",
        extra={
            "provider_type": provider_type,
            "num_of_alerts": len(formatted_events),
            "provider_id": provider_id,
            "tenant_id": tenant_id,
        },
    )
    try:
        for formatted_event in formatted_events:
            formatted_event.pushed = True
            alert = Alert(
                tenant_id=tenant_id,
                provider_type=provider_type,
                event=formatted_event.dict(),
                provider_id=provider_id,
                fingerprint=formatted_event.fingerprint,
            )
            session.add(alert)
            formatted_event.event_id = alert.id
            try:
                pusher_client.trigger(
                    f"private-{tenant_id}",
                    "async-alerts",
                    base64.b64encode(
                        zlib.compress(json.dumps([alert.event]).encode(), level=9)
                    ).decode(),
                )
            except Exception:
                logger.exception("Failed to push alert to the client")
        session.commit()
        logger.info(
            "Asyncronusly added new alerts to the DB",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
    except Exception:
        logger.exception(
            "Failed to push alerts to the DB",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
    try:
        # Now run any workflow that should run based on this alert
        # TODO: this should publish event
        workflow_manager = WorkflowManager.get_instance()
        # insert the events to the workflow manager process queue
        logger.info("Adding events to the workflow manager queue")
        workflow_manager.insert_events(tenant_id, formatted_events)
        logger.info("Added events to the workflow manager queue")
    except Exception:
        logger.exception(
            "Failed to run workflows based on alerts",
            extra={
                "provider_type": provider_type,
                "num_of_alerts": len(formatted_events),
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )


@router.post(
    "/event",
    description="Receive a generic alert event",
    response_model=AlertDto | list[AlertDto],
    status_code=201,
)
async def receive_generic_event(
    alert: AlertDto | list[AlertDto],
    bg_tasks: BackgroundTasks,
    tenant_id: str = Depends(verify_api_key),
    session: Session = Depends(get_session),
    pusher_client: Pusher = Depends(get_pusher_client),
):
    """
    A generic webhook endpoint that can be used by any provider to send alerts to Keep.

    Args:
        alert (AlertDto | list[AlertDto]): The alert(s) to be sent to Keep.
        bg_tasks (BackgroundTasks): Background tasks handler.
        tenant_id (str, optional): Defaults to Depends(verify_api_key).
        session (Session, optional): Defaults to Depends(get_session).
    """
    if isinstance(alert, AlertDto):
        alert = [alert]
    bg_tasks.add_task(
        handle_formatted_events,
        tenant_id,
        alert[0].source[0] or "keep",
        session,
        alert,
        pusher_client,
    )
    return alert


@router.post(
    "/event/{provider_type}", description="Receive an alert event from a provider"
)
async def receive_event(
    provider_type: str,
    request: Request,
    bg_tasks: BackgroundTasks,
    provider_id: str | None = None,
    tenant_id: str = Depends(verify_api_key),
    session: Session = Depends(get_session),
    pusher_client: Pusher = Depends(get_pusher_client),
) -> dict[str, str]:
    provider_class = ProvidersFactory.get_provider_class(provider_type)
    # if this request is just to confirm the sns subscription, return ok
    # TODO: think of a more elegant way to do this
    # Get the raw body as bytes
    body = await request.body()
    # Parse the raw body
    body = provider_class.parse_event_raw_body(body)
    # Start process the event
    # Attempt to parse as JSON if the content type is not text/plain
    # content_type = request.headers.get("Content-Type")
    # For example, SNS events (https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.prepare.html)
    try:
        event = json.loads(body.decode())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # else, process the event
    logger.info(
        "Handling event",
        extra={
            "provider_type": provider_type,
            "provider_id": provider_id,
            "tenant_id": tenant_id,
        },
    )
    try:
        # Each provider should implement a format_alert method that returns an AlertDto
        # object that will later be returned to the client.
        logger.info(
            f"Trying to format alert with {provider_type}",
            extra={
                "provider_type": provider_type,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
        # tb: if we want to have fingerprint_fields configured by the user, format_alert
        #   needs to be called from an initalized provider instance instead of a static method.
        formatted_events = provider_class.format_alert(event)

        if isinstance(formatted_events, AlertDto):
            formatted_events = [formatted_events]

        logger.info(
            f"Formatted alerts with {provider_type}",
            extra={
                "provider_type": provider_type,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
        # If the format_alert does not return an AlertDto object, it means that the event
        # should not be pushed to the client.
        if formatted_events:
            bg_tasks.add_task(
                handle_formatted_events,
                tenant_id,
                provider_type,
                session,
                formatted_events,
                pusher_client,
                provider_id,
            )
        logger.info(
            "Handled event successfully",
            extra={
                "provider_type": provider_type,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
            },
        )
        return {"status": "ok"}
    except Exception as e:
        logger.exception(
            "Failed to handle event", extra={"error": str(e), "tenant_id": tenant_id}
        )
        raise HTTPException(400, "Failed to handle event")


@router.get(
    "/{fingerprint}",
    description="Get alert by fingerprint",
)
def get_alert(
    background_tasks: BackgroundTasks,
    fingerprint: str,
    tenant_id: str = Depends(verify_token_or_key),
    session: Session = Depends(get_session),
) -> AlertDto:
    logger.info(
        "Fetching alert",
        extra={
            "fingerprint": fingerprint,
            "tenant_id": tenant_id,
        },
    )
    # TODO: once pulled alerts will be in the db too, this should be changed
    pushed_alerts = _get_alerts_from_db(tenant_id=tenant_id)
    pulled_alerts = _get_alerts_from_providers(tenant_id=tenant_id)
    all_alerts = pushed_alerts + pulled_alerts
    alert = list(filter(lambda alert: alert.fingerprint == fingerprint, all_alerts))
    if alert:
        return alert[0]
    else:
        raise HTTPException(status_code=404, detail="Alert not found")


@router.post(
    "/enrich",
    description="Enrich an alert",
)
def enrich_alert(
    enrich_data: EnrichAlertRequestBody,
    tenant_id: str = Depends(verify_token_or_key),
) -> dict[str, str]:
    logger.info(
        "Enriching alert",
        extra={
            "fingerprint": enrich_data.fingerprint,
            "tenant_id": tenant_id,
        },
    )

    try:
        enrich_alert_db(
            tenant_id=tenant_id,
            fingerprint=enrich_data.fingerprint,
            enrichments=enrich_data.enrichments,
        )

        logger.info(
            "Alert enriched successfully",
            extra={"fingerprint": enrich_data.fingerprint, "tenant_id": tenant_id},
        )
        return {"status": "ok"}

    except Exception as e:
        logger.exception("Failed to enrich alert", extra={"error": str(e)})
        return {"status": "failed"}


@router.get(
    "/{provider_type}/{alert_id}/terraform",
    description="Get alert terraform definition",
)
def get_alert_terraform(
    alert_id: str,
    # TODO: support provider_id instead of provider_type (the problem is that when alert is pushed, we don't have the provider_id but only the type)
    provider_type: str,
    tenant_id: str = Depends(verify_token_or_key),
    session: Session = Depends(get_session),
) -> dict:
    # TODO: more than one source
    provider = ProvidersFactory.get_installed_providers(
        tenant_id=tenant_id, provider_type=provider_type
    )
    if len(provider) == 0:
        logger.error(f"Provider {provider_type} is not installed")
        return {"error": "Provider is not installed"}
    # TODO: support more than one provider within the same type
    #       the problem is that the alert doesn't have correlation for the provider id
    #       (for more context, Shahar)
    elif len(provider) > 1:
        logger.error(f"More than one provider of type {provider_type} is installed")
        return {"error": "More than one provider of type is installed"}
    # init the provider itself
    provider = ProvidersFactory.get_provider(
        context_manager=ContextManager(tenant_id=tenant_id),
        provider_id=provider[0].id,
        provider_type=provider_type,
        provider_config=provider[0].details,
    )
    provider_terraformer_args = provider.get_terraformer_args(alert_id)

    if not provider_terraformer_args:
        logger.error(f"Provider {provider_type} doesn't support Terraformer")
        return {"error": "Provider doesn't support Terraformer"}

    # TODO: maybe add TerraformerArgs or something
    terraformer_filter = provider_terraformer_args.get("filter")
    terraform_auth = provider_terraformer_args.get("auth")
    terraform_resource_type = provider_terraformer_args.get("resource")

    if not terraformer_filter or not terraform_auth or not terraform_resource_type:
        logger.error(f"Provider {provider_type} doesn't support Terraformer")
        return {"error": "Provider doesn't support Terraformer"}

    tmp_output_dir = f"/tmp/terraformer_output_{alert_id}"
    os.makedirs(tmp_output_dir, exist_ok=True)
    cmd = [
        "terraformer",
        "import",
        f"{provider_type}",
        f"--resources={terraform_resource_type}",
        f'--filter="{terraformer_filter}"',
        f"--path-output={tmp_output_dir}",
    ]
    # add the auth args
    for auth in terraform_auth:
        cmd.append(f"{auth}={terraform_auth[auth]}")

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        logger.error(f"Terraformer error: {stderr.decode('utf-8')}")
        raise HTTPException(status_code=500, detail="Terraformer error")

    if "404 Not Found" in stdout.decode("utf-8"):
        logger.error(f"Terraformer error: {stderr.decode('utf-8')}")
        raise HTTPException(status_code=404, detail="Resource not found")

    # get the monitor
    with open(
        f"{tmp_output_dir}/{provider_type}/{terraform_resource_type}/{terraform_resource_type}.tf",
        "r",
    ) as f:
        terraform_definition = f.read()
    # Cleanup the temporary directory
    shutil.rmtree(tmp_output_dir)

    logger.info(
        "Fetching alert terraform definition",
        extra={
            "alert_id": alert_id,
            "tenant_id": tenant_id,
        },
    )
    return {"terraform_definition": terraform_definition}
