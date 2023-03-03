#set -x
curl -X GET -s "https://amd64.ocp.releases.ci.openshift.org/dashboards/compare?from=4.11.9&to=4.11.28&format=json" -H "Accept: application/json" | grep -zoP '(?<=<code>)(?s).*(?=</pre></code>)' > compare.txt
#packageArray=( $(jq -r .updatedImages[].name compare.txt))
mapfile -t packageArray < <(jq -r '.updatedImages[].name' compare.txt)
for package in "${packageArray[@]}"
do IFS=$'\n'
   echo ${package^^}
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