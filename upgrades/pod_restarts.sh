#!/bin/bash
#set -x
# Get all pods with restarts greater than 10
PODS=$(omc get pods -A | awk '{if ($5>10) print $2 "," $1}')

# Loop through each pod and get its details
for pod in ${PODS[@]}; do
    NAMESPACE=$(echo $pod | cut -d',' -f2)
    POD=$(echo $pod | cut -d',' -f1)
    echo $POD in $NAMESPACE
    omc get pods $POD -n $NAMESPACE -o jsonpath='{.status.containerStatuses[*].lastState}' | jq
done
