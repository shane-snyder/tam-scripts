#!/bin/bash
for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`
do 
    if [ $(omc logs $POD -c etcd -n openshift-etcd| grep 'failed to send out heartbeat on time'| wc -l) -gt 0 ]
    then
        echo -e "\e[1;31mETCD HEARTBEAT\e[1;m \nAs etcd uses a leader-based consensus protocol for consistent data replication and log execution, it relies in a heartbeat mechnism to keep the cluster members in a healthy state. \n"
        echo -e "RESOURCES
        https://access.redhat.com/solutions/4865321
        https://etcd.io/docs/v3.5/faq/#what-does-the-etcd-warning-failed-to-send-out-heartbeat-on-time-mean \n"
        omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'failed to send out heartbeat on time'| cut -c -16 | uniq -c; done
        echo -e "\n"
        break
    else 
        echo -e "\e[1;32mETCD HEARTBEAT IS GOOD\e[1;m\n"
        break
    fi
done

for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`
do 
    if [ $(omc logs $POD -c etcd -n openshift-etcd| grep "server is likely overloaded" | wc -l) -gt 0 ]
    then
        echo -e "\e[1;31mETCD IS LIKELY OVERLOADED\e[1;m \netcd heartbeat messages are followed by server is likely overloaded"
        omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep "server is likely overloaded"| cut -c -16 | uniq -c; done
        echo -e "\n"
        break
    else 
        echo -e "\e[1;32mETCD IS NOT OVERLOADED\e[1;m \n"
        break
    fi
done

for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`
do 
    if [ $(omc logs $POD -c etcd -n openshift-etcd| grep 'took too long' | wc -l) -gt 0 ]
    then
        echo -e "\e[1;31mETCD WARNING APPLY ENTRIES TOOK TOO LONG\e[1;m \nAs per required by its consensus protocol implementation, after a majority of etcd members agree to commit a request, each etcd server applies the request to its data store and persists the result to disk. If the average request duration exceeds 100 milliseconds, etcd will warn entries request took too long"
        omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'took too long'| cut -c -16 | uniq -c; done
        echo -e "\n"
        break
    else 
        echo -e "\e[1;32mAPPLY TIMES ARE OK\e[1;m \n"
        break
    fi
done

for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`
do 
    if [ $(omc logs $POD -c etcd -n openshift-etcd| grep 'clock difference'| wc -l) -gt 0 ]
    then
        echo -e "\e[1;31mETCD CLOCK DIFFERENCE\e[1;m\nAs per required by its consensus protocol implementation, after a majority of etcd members agree to commit a request, each etcd server applies the request to its data store and persists the result to disk. If the average request duration exceeds 100 milliseconds, etcd will warn entries request took too long"
        omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'clock difference'| cut -c -16 | uniq -c; done
        echo -e "\n"
    else 
        echo -e "\e[1;32mETCD CLOCK IS OK\e[1;m \n"
        break
    fi
done

for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`
do 
    if [ $(omc logs $POD -c etcd -n openshift-etcd| grep 'database space exceeded'| wc -l) -gt 0 ]
    then
        echo -e "\e[1;31mETCD DATABASE SPACE EXCEEDED\e[1;m \nWithout periodically compacting this history (e.g., by setting --auto-compaction), etcd will eventually exhaust its storage space. If etcd runs low on storage space, it raises a space quota alarm to protect the cluster from further writes. So long as the alarm is raised, etcd responds to write requests with the error mvcc: database space exceeded.
        In RHOCP 4.x, history compaction is performed automatically every five minutes and leaves gaps in the back-end database. This fragmented space is available for use by etcd, but is not available to the host file system. You must defragment etcd to make this space available to the host file system.
        Starting in RHOCP 4.9.z, defragmentation occurs automatically."
        omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'database space exceeded'| cut -c -16 | uniq -c; done
        echo -e "\n"
    else 
        echo -e "\e[1;32mETCD DATABASE SPACE HAS NOT EXCEEDED\e[1;m \n"
        break
    fi
done

for POD in `omc get pods -n openshift-etcd|grep etcd|grep -v quorum`
do 
    if [ $(omc logs $POD -c etcd -n openshift-etcd| grep 'leader changed'| wc -l) -gt 0 ]
    then
        echo -e "\e[1;31mETCD LEADERSHIP CHANGES AND FAILURES\e[1;m\nLeadership changes are expected only during installations, upgrades or machine config operations. During day to day operation, it must be avoided.
        During the leader election the cluster cannot process any writes. Write requests sent during the election are queued for processing until a new leader is elected. Until a new leader is elected, we are going to observe instabilities, slow response times and unexpected behaviors affecting RHOCP control plane."
        omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'leader changed'| cut -c -16 | uniq -c; done
        echo -e "\n"
    else 
        echo -e "\e[1;32mETCD HAS NO LEADERSHIP CHANGES OR FAILURES\e[1;m \n"
        break
    fi
done