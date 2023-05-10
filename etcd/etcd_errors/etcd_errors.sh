#!/bin/bash
#set -x

#OPTIONS
get_help(){
cat  <<HELP
Options:
  -oc, --oc:      Directs script to run on an OpenShift cluster
  -M, --month:    Displays error occurances by month
  -d, --day:      Displays error occurances by day
  -H, --hour:     Displays error occurances by hour
  -m, --minute:   Displays error occurances by minute
  -h, --help:     Displays options

HELP
}

#SET DEFAULT ERROR OCCURANCE COUNT TO BY DAY
TIME=10
PLOT_DATE="%Y-%m-%d"

#SET DEFAULT CLIENT TO OMC
CLIENT="omc"

#SELECT TIMEFRAME TO DISPLAY ETCD ERROR OCCURANCE COUNT
PARAMS=""
while (( "$#" )); do
  case "$1" in
    -M|--month)
      TIME=7
      shift
      ;;
    -d|--day)
      TIME=10
      shift
      ;;
    -H|--hour)
      TIME=13
      shift
      ;;
    -m|--minute)
      TIME=16
      shift
      ;;
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
      updated_mg=`cat ~/.omc/omc.json | jq -r '.contexts[] | select (.current=="*") | .path'`
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

eval set -- "$PARAMS"

failed_to_send_out_heartbeat_on_time(){
echo -e "\e[1;31mETCD HEARTBEAT\e[1;m \nAs etcd uses a leader-based consensus protocol for consistent data replication and log execution, it relies in a heartbeat mechnism to keep the cluster members in a healthy state. \n"
            echo -e "RESOURCES
            https://access.redhat.com/solutions/4865321
            https://etcd.io/docs/v3.5/faq/#what-does-the-etcd-warning-failed-to-send-out-heartbeat-on-time-mean \n"
}

server_is_likely_overloaded(){
echo -e "\e[1;31mETCD IS LIKELY OVERLOADED\e[1;m \netcd heartbeat messages are followed by server is likely overloaded"

}

took_too_long(){
echo -e "\e[1;31mETCD WARNING APPLY ENTRIES TOOK TOO LONG\e[1;m \nAs per required by its consensus protocol implementation, after a majority of etcd members agree to commit a request, each etcd server applies the request to its data store and persists the result to disk. If the average request duration exceeds 100 milliseconds, etcd will warn entries request took too long"
        echo -e "RESOURCES
        https://etcd.io/docs/v3.5/faq/#what-does-the-etcd-warning-apply-entries-took-too-long-mean \n"
}

clock_difference(){
echo -e "\e[1;31mETCD CLOCK DIFFERENCE\e[1;m\nAs per required by its consensus protocol implementation, after a majority of etcd members agree to commit a request, each etcd server applies the request to its data store and persists the result to disk. If the average request duration exceeds 100 milliseconds, etcd will warn entries request took too long"
        echo -e "RESOURCES
        https://access.redhat.com/solutions/6371021
        https://docs.openshift.com/container-platform/4.6/installing/install_config/installing-customizing.html?extIdCarryOver=true&sc_cid=701f2000001Css5AAC#installation-special-config-chrony_installing-customizing \n"
}

database_space_exceeded(){
echo -e "\e[1;31mETCD DATABASE SPACE EXCEEDED\e[1;m \nWithout periodically compacting this history (e.g., by setting --auto-compaction), etcd will eventually exhaust its storage space. If etcd runs low on storage space, it raises a space quota alarm to protect the cluster from further writes. So long as the alarm is raised, etcd responds to write requests with the error mvcc: database space exceeded.
        In RHOCP 4.x, history compaction is performed automatically every five minutes and leaves gaps in the back-end database. This fragmented space is available for use by etcd, but is not available to the host file system. You must defragment etcd to make this space available to the host file system.
        Starting in RHOCP 4.9.z, defragmentation occurs automatically."
        echo -e "RESOURCES
        https://etcd.io/docs/v3.5/faq/#what-does-mvcc-database-space-exceeded-mean-and-how-do-i-fix-it \n"
        
}

leader_changed(){
echo -e "\e[1;31mETCD LEADERSHIP CHANGES AND FAILURES\e[1;m\nLeadership changes are expected only during installations, upgrades or machine config operations. During day to day operation, it must be avoided.
        During the leader election the cluster cannot process any writes. Write requests sent during the election are queued for processing until a new leader is elected. Until a new leader is elected, we are going to observe instabilities, slow response times and unexpected behaviors affecting RHOCP control plane."
        echo -e "RESOURCES
        https://etcd.io/docs/v3.5/op-guide/failures/#leader-failure \n"
        
}
check_clients
etcd_errors=("failed to send out heartbeat on time" "server is likely overloaded" "took too long" "clock difference" "database space exceeded" "leader changed")
etcd_recomendations=(heartbeat)
for i in "${!etcd_errors[@]}"
do 
    for POD in `$CLIENT get pods -n openshift-etcd|grep etcd|grep -v quorum`; do
        if [ $($CLIENT logs $POD -c etcd -n openshift-etcd| grep "${etcd_errors[$i]}"| wc -l) -gt 0 ]
        then
            function=${etcd_errors[$i]// /_}
            ${function}
            $CLIENT get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD
            $CLIENT logs $POD -c etcd -n openshift-etcd| grep "${etcd_errors[$i]}"| cut -c -$TIME | uniq -c

            # Run gnuplot only if the function is "took_too_long"
                if [ "$function" == "took_too_long" ]; then
                $CLIENT logs $POD -c etcd -n openshift-etcd| grep "${etcd_errors[$i]}"| cut -c -10 | uniq -c > plot.txt
                    input_file="plot.txt"
                    output_file="plot_formatted.txt"
                    start_date=$(head -n 1 "$input_file" | awk '{print $2}')
                    end_date=$(tail -n 1 "$input_file" | awk '{print $2}')

                    # Generate a complete list of dates between start_date and end_date
                    all_dates=()
                    current_date="$start_date"
                    while [ "$current_date" != "$end_date" ]; do
                        all_dates+=("$current_date")
                        current_date=$(date -I -d "$current_date + 1 day")
                    done
                    all_dates+=("$end_date")

                    # Process the input file and output the counts with missing dates filled with 0
                    declare -A counts
                    while read -r count date; do
                        counts["$date"]=$count
                    done < "$input_file"

                    rm "$output_file" 2>/dev/null
                    for date in "${all_dates[@]}"; do
                        if [ -n "${counts["$date"]}" ]; then
                            echo "${counts["$date"]} $date" >> "$output_file"
                        else
                            echo "0 $date" >> "$output_file"
                        fi
                    done
                    gnuplot << EOF
                    set xdata time
                    set timefmt "${PLOT_DATE}"
                    set format x "%Y-%m-%d"
                    set xlabel "Date"
                    set ylabel "Number of Occurrences"
                    set grid
                    set title "Occurrences vs. Date"
                    set terminal pngcairo size 1800,600 enhanced font 'Verdana,10'
                    set output "${POD}.png"
                    plot "plot_formatted.txt" using 2:1 with lines title "Occurrences" lw 2 lc rgb "red", \
                         "plot_formatted.txt" using 2:1 with points pt 7 ps 1.2 lc rgb "black" title ""
EOF
                rm plot.txt
                cat plot_formatted.txt
                fi
            done
            echo -e "\n"
            break
        else 
            echo -e "\e[1;32mThere are no errors for ${etcd_errors[$i]} found\e[1;m \n"
            ((i++))
            break
        fi
    done
done
