#!/bin/bash

# Set variables
grafana_version="9.1.5"
arch=$(uname -m | tr '[:upper:]' '[:lower:]')
os=$(uname -s | tr '[:upper:]' '[:lower:]')

# Download and unpack Grafana
mkdir /tmp/grafana/
curl -Ls "https://dl.grafana.com/oss/release/grafana-${grafana_version}.${os}-${arch}.tar.gz" | tar -xvz --strip-components=1 -C /tmp/grafana

# Move to the Grafana directory
cd /tmp/grafana/bin/

# Start Grafana server
./grafana-server

echo "Grafana is running. Access it at http://localhost:3000"