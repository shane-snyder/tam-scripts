#!/bin/bash
echo -e "ETCD HEARTBEAT \nAs etcd uses a leader-based consensus protocol for consistent data replication and log execution, it relies in a heartbeat mechnism to keep the cluster members in a healthy state."
omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'failed to send out heartbeat on time'| wc -l; done
echo -e "\n"

echo -e "ETCD IS LIKELY OVERLOADED \netcd heartbeat messages are followed by server is likely overloaded"
omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep "server is likely overloaded"| wc -l; done
echo -e "\n"

echo -e "ETCD WARNING “APPLY ENTRIES TOOK TOO LONG \nAs per required by its consensus protocol implementation, after a majority of etcd members agree to commit a request, each etcd server applies the request to its data store and persists the result to disk. If the average request duration exceeds 100 milliseconds, etcd will warn entries request took too long"
omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'took too long'| wc -l; done
echo -e "\n"

echo -e "ETCD CLOCK DIFFERENCE\nAs per required by its consensus protocol implementation, after a majority of etcd members agree to commit a request, each etcd server applies the request to its data store and persists the result to disk. If the average request duration exceeds 100 milliseconds, etcd will warn entries request took too long"
omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'clock difference'| wc -l; done
echo -e "\n"

echo -e "ETCD DATABASE SPACE EXCEEDED\nWithout periodically compacting this history (e.g., by setting --auto-compaction), etcd will eventually exhaust its storage space. If etcd runs low on storage space, it raises a space quota alarm to protect the cluster from further writes. So long as the alarm is raised, etcd responds to write requests with the error mvcc: database space exceeded.

In RHOCP 4.x, history compaction is performed automatically every five minutes and leaves gaps in the back-end database. This fragmented space is available for use by etcd, but is not available to the host file system. You must defragment etcd to make this space available to the host file system.

Starting in RHOCP 4.9.z, defragmentation occurs automatically."
omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'database space exceeded'| wc -l; done
echo -e "\n"

echo -e "ETCD LEADERSHIP CHANGES AND FAILURES\nLeadership changes are expected only during installations, upgrades or machine config operations. During day to day operation, it must be avoided.
During the leader election the cluster cannot process any writes. Write requests sent during the election are queued for processing until a new leader is elected. Until a new leader is elected, we are going to observe instabilities, slow response times and unexpected behaviors affecting RHOCP control plane."
omc get pods -n openshift-etcd|grep etcd|grep -v quorum|while read POD line; do echo $POD && omc logs $POD -c etcd -n openshift-etcd| grep 'leader changed'| wc -l; done
echo -e "\n"