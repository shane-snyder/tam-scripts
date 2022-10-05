#!/bin/bash
#Cluster ID
export CLUSTER_ID=`omc get clusterversion -o jsonpath='{.items[].spec.clusterID}{"\n"}{"\n"}'`
export CHANNEL=`omc get clusterversion -o jsonpath='{.items[].spec.channel}{"\n"}{"\n"}'`
export VERSION=`omc get clusterversion -o jsonpath='{.items[].spec.desiredUpdate.version}{"\n"}{"\n"}'`
#Cluster ID
echo "Cluster ID: $CLUSTER_ID"

#Channel
echo "Channel: $CHANNEL"

#Desired version
echo "Current version: $VERSION"

#Print API calls using deprecated APIs
echo "APIs removed in release"
omc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!="")]}{.status.removedInRelease}{"\t"}{.status.requestCount}{"\t"}{.metadata.name}{"\n"}{end}' | column -t -NREMOVEDINRELEASE,REQUESTSINLAST24H,NAME

#Loop through deprecated APIs with calls in last 24 hours greater than 0 and print the Username and Agent making calls
echo -e "\nAPI calls"
for apps in `omc get apirequestcounts -o json | jq -r '.items[] | select((.status.removedInRelease!=null) and .status.requestCount!=0) | .metadata.name'`;
do echo -e ${apps^^} && omc get apirequestcounts $apps -o json | jq -r '.items[].status.last24h[].byNode[]? | select(.byUser!=null) | .byUser[].username + "\t" + .byUser[].userAgent' | sort | uniq | column -t -N,SERVICEACCOUNT,AGENT && echo -e "\n";
done

#CSV
#Print API calls using deprecated APIs
echo "APIs removed in release" >> test.csv
omc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!="")]}{.status.removedInRelease}{","}{.status.requestCount}{","}{.metadata.name}{"n"}{end}' | column -t -NREMOVEDINRELEASE,REQUESTSINLAST24H,NAME >> test.csv

#Loop through deprecated APIs with calls in last 24 hours greater than 0 and print the Username and Agent making calls
echo -e "\nAPI calls" >> test.csv
for apps in `omc get apirequestcounts -o json | jq -r '.items[] | select((.status.removedInRelease!=null) and .status.requestCount!=0) | .metadata.name'`;
do echo -e ${apps^^} && omc get apirequestcounts $apps -o json | jq -r '.items[].status.last24h[].byNode[]? | select(.byUser!=null) | .byUser[].username + "," + .byUser[].userAgent' | sort | uniq | column -t -N,SERVICEACCOUNT,AGENT >> test.csv && echo -e "\n";
done
