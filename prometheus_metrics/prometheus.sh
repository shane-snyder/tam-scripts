#/bin/bash

#Install prometheus
#set -x
arch=$(uname -m | tr '[:upper:]' '[:lower:]')
os=$(uname -s | tr '[:upper:]' '[:lower:]')
prometheus_version="2.52.0"

echo $arch
echo $os

mkdir /tmp/prometheus/
curl -Ls "https://github.com/prometheus/prometheus/releases/download/v${prometheus_version}/prometheus-${prometheus_version}.${os}-${arch}.tar.gz" | tar -xvz --strip-components=1 -C /tmp/prometheus
mkdir /tmp/prometheus/data
#mkdir /tmp/prometheus/prometheus-$prometheus_version.$os-$arch/data
read -p "Enter the path to the Prometheus DB dump: " db_dump_path
tar -xvf "$db_dump_path" -C /tmp/prometheus
for file in /tmp/prometheus/metrics/*.gz; do tar -xvzf "$file" -C /tmp/prometheus/data; done


echo "Launching prometheus. This could take awhile depending on how large the dump was."
cd /tmp/prometheus
./prometheus --config.file=/tmp/prometheus/prometheus.yml