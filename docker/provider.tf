terraform {
  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "3.20.0"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "2.6.1"
    }
    dynatrace = {
      source  = "dynatrace-oss/dynatrace"
      version = "1.47.2"
    }
    ec = {
      source  = "elastic/ec"
      version = "0.9.0"
    }
    newrelic = {
      source  = "newrelic/newrelic"
      version = "3.27.7"
    }
  }
}

provider "grafana" {
  # Configuration options
}

provider "dynatrace" {
  # Configuration options
}

provider "ec" {
  # Configuration options
}

provider "newrelic" {
  # Configuration options
}

provider "datadog" {
  # Configuration options
}
