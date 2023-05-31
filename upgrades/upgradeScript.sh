#!/bin/bash
#set -x
#SET DEFAULT CLIENT TO OMC
CLIENT="omc"

PARAMS=""
while (( "$#" )); do
  case "$1" in
    -oc|--oc)
      CLIENT="oc"
      shift
      ;;
    -h|--help)
      get_help
      shift
      exit 0
      ;;
    -*|--*=) # unsupported flags
      echo "Error: Unsupported flag $1" >&2
      exit 1
      ;;
    *) # preserve positional arguments
      PARAMS="$PARAMS $1"
      shift
      ;;
  esac
done
eval set -- "$PARAMS"

#Check which client is being used
check_clients(){
if [ $CLIENT = "omc" ]; then
  if [ ! $(command -v omc) ]; then
    echo "Openshift must-gather client not found. Please install omc."
    echo "https://github.com/gmeghnag/omc"
    exit 1
    else
    current_mg=`cat ~/.omc/omc.json | jq -r '.contexts[] | select (.current=="*") | .path'`
    echo "Enter full path to must-gather you'd like to use or press enter to use current must-gather Current must-gather: "$current_mg
    read line
    if [ ! $line ]; then
      echo "Using current must-gather"
      else
      omc use ${line}
      updated_mg=`jq -r '.contexts[] | select (.current=="*") | .path' ~/.omc/omc.json`
          if [ "$current_mg" == "$updated_mg" ]; then
          echo "Must-gather was not switched. Please ensure you're using the full path"
          exit 1;
          else
          echo "Must-gather switched to $line"
          omc use
          fi
      fi
  fi
  elif [ $CLIENT = "oc" ]; then
    if [ ! $(command -v oc) ]; then
    echo "Download oc"
    exit 1;
    else
    echo "Logged into" `oc whoami --show-server=true` "as user" `oc whoami`
    fi
fi
}

set_versions() {
   CURRENT_VERSION=$($CLIENT get clusterversion version -o jsonpath='{.status.desired.version}')
   echo "Current version is $CURRENT_VERSION"
   read -p "Enter version you are updating to : " NEW_VERSION
   echo $NEW_VERSION

}

deprecated_apis() {
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
}
compare_packages() {
curl -X GET -s "https://amd64.ocp.releases.ci.openshift.org/dashboards/compare?from=$CURRENT_VERSION&to=$NEW_VERSION&format=json" -H "Accept: application/json" | grep -zoP '(?<=<code>)(?s).*(?=</pre></code>)' > compare.txt
#packageArray=( $(jq -r .updatedImages[].name compare.txt))
mapfile -t packageArray < <(jq -r '.updatedImages[].name' compare.txt)
for package in "${packageArray[@]}"
do IFS=$'\n'
   echo ${package^^}
   jq -r '.updatedImages[] | select (.name=="'${package}'") | ("Path: " + "\(.path)")' compare.txt
   jq -r '.updatedImages[] | select (.name=="'${package}'") | ("Full change log: " + "\(.fullChangeLog)")' compare.txt
   FIND_BUGS=`jq -r '.updatedImages[] | select (.name=="'${package}'") | ("\(.commits[].issues[]?)")' compare.txt`
   if [ -n "$FIND_BUGS" ]
   then
    echo "Bugs"
    echo -e "$FIND_BUGS\n"
   fi
   FIND_UPDATES=`jq -r '.updatedImages[] | select (.name=="'${package}'") | .commits[] | "\(.subject)"' compare.txt`
   if [ -n "$FIND_UPDATES" ]
   then
    echo "Updates"
    jq -r '.updatedImages[] | select (.name=="'${package}'") | .commits[] | "\(.subject)" + " -> " + "\(.pullURL)"' compare.txt
   fi
   echo -e "\n"
done
rm compare.txt
}

check_clients
set_versions
deprecated_apis | tee apis.txt
compare_packages | tee packages.txt
  
