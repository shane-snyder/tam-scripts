#!/bin/bash
#set -x
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

etcd_errors=("failed to send out heartbeat on time" "server is likely overloaded" "took too long" "clock difference" "database space exceeded" "leader changed")
etcd_recomendations=(heartbeat)
for i in "${!etcd_errors[@]}"
do 
    for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`; do
        if [ $(omc logs $POD -c etcd -n openshift-etcd| grep "${etcd_errors[$i]}"| wc -l) -gt 0 ]
        then
            function=${etcd_errors[$i]// /_}
            ${function}
            omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep "${etcd_errors[$i]}"| cut -c -7 | uniq -c; done
            echo -e "\n"
            break
        else 
            echo -e "\e[1;32mThere are no errors for ${etcd_errors[$i]} found\e[1;m \n"
            ((i++))
            break
        fi
    done
done

