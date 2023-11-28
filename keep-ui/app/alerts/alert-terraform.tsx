import { Dialog, Transition } from "@headlessui/react";
import { Fragment, useEffect, useState } from "react";
import { Button, Flex, Title, Divider } from "@tremor/react";
import { Alert } from "./models";
import Loading from "app/loading"; // Ensure this is the correct path to your Loading component
import { getApiURL } from "utils/apiUrl";
import { getSession } from "next-auth/react";
import { CopyBlock, nord } from "react-code-blocks"; // Import CopyBlock

interface AlertTerraformProps {
  isOpen: boolean;
  closeModal: () => void;
  alert: Alert;
}

export function AlertTerraform({ isOpen, closeModal, alert }: AlertTerraformProps) {
  const [terraformDefinition, setTerraformDefinition] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isDownloadDisabled, setIsDownloadDisabled] = useState(true);

  useEffect(() => {
    if (!isOpen || !alert) return; // Only fetch when the modal is opened and alert is not null

    if (terraformDefinition) {
      return;
    }
    const fetchTerraformDefinition = async () => {
      setIsLoading(true); // Start loading
      try {
        if (!alert.source || alert.source.length === 0) {
          throw new Error('Alert does not have a source');
        }
        // TODO: support alerts with more than one source
        const alertSource = alert.source[0];
        const session = await getSession();
        const apiUrl = getApiURL();
        const response = await fetch(`${apiUrl}/alerts/${alertSource}/${alert.resourceId}/terraform`,
          {
            method: "GET",
            headers: {
              Authorization: `Bearer ${session!.accessToken}`,
              "Content-Type": "application/json",
            },
          },
        );
        if (!response.ok) {
          throw new Error('Network response was not ok');
        }
        const data = await response.json();
        setTerraformDefinition(data.terraformDefinition);
        setIsDownloadDisabled(false); // Enable the Download button
      } catch (error) {
        console.error('Error fetching terraform definition:', error);
        // You may also want to handle this error in the UI
      } finally {
        setIsLoading(false); // Stop loading
      }
    };

    fetchTerraformDefinition();
  }, [isOpen, alert?.id, alert?.source]); // Include alert properties in the dependency array

  const downloadTerraformFile = () => {
    const blob = new Blob([terraformDefinition], { type: 'text/plain' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'monitor.tf';
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
  };

  return (
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="fixed inset-0 z-50 overflow-y-auto" onClose={closeModal}>
        <div className="flex items-center justify-center min-h-screen p-4 text-center">
          <Transition.Child
            as={Fragment}
            enter="ease-out duration-300"
            enterFrom="opacity-0"
            enterTo="opacity-100"
            leave="ease-in duration-200"
            leaveFrom="opacity-100"
            leaveTo="opacity-0"
          >
            <div className="fixed inset-0 transition-opacity">
              <div className="absolute inset-0 bg-gray-900 opacity-75"></div>
            </div>
          </Transition.Child>
          <span className="hidden sm:inline-block sm:align-middle sm:h-screen"></span>
          &#8203;
          <Transition.Child
            as={Fragment}
            enter="ease-out duration-300"
            enterFrom="opacity-0 scale-95"
            enterTo="opacity-100 scale-100"
            leave="ease-in duration-200"
            leaveFrom="opacity-100 scale-100"
            leaveTo="opacity-0 scale-95"
          >
            <div className="w-full max-w-4xl p-6 my-8 overflow-hidden text-left align-middle transition-all transform bg-white shadow-xl rounded-xl sm:w-full sm:max-w-2xl">
              <Title>Terraform Definition</Title>
              <Divider />
              {isLoading ? (
                <Loading includeMinHeight={false} /> // Display loading indicator
              ) : (
                <div className="mt-4">
                    <pre className="whitespace-pre-wrap text-sm text-gray-700">
                      <code>
                        {terraformDefinition || 'No definition available.'}
                      </code>
                    </pre>
                </div>
              )}
              <Divider />
              <Flex justifyContent="end" className="mt-4">
                <Button
                  color="orange"
                  variant="secondary"
                  onClick={closeModal}
                >
                  Close
                </Button>
                <Button
                  color="orange"
                  className="ml-2"
                  onClick={downloadTerraformFile}
                  disabled={isDownloadDisabled}
                >
                  Download
                </Button>
              </Flex>
            </div>
          </Transition.Child>
        </div>
      </Dialog>
    </Transition>
  );
}
