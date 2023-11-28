terraform {
  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "3.20.0"
    }
  }
}

provider "datadog" {
  # Configuration options
}
