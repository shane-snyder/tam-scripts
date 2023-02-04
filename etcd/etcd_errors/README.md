# Etd errors script
The following script assess common errors with etd. This can be ran on a must-gather utilizing omc or ran on a running OpenShift cluster using the oc cli.

## How to use on a must-gather
1. Ensure [omc](https://github.com/gmeghnag/omc) is installed on your local machine.
2. By default, the script uses omc to read on a must-gather rather than a running cluster. You simply execute the script.
3. You'll be prompted to enter the must-gather you'd like to analyze. Enter the full path of the must-gather or press enter to continue with the must-gather that omc is already directed to use.

## How to use on a running cluster via the oc cli
1. Install the oc cli
2. Use oc login to sign in to the cluster
3. Execute the script with the -oc parameter
Example:
> ./etcd_errors.sh -oc

## How to sort errors by time
By default, the script will provide you with the number of occurances for each error is printed per day. You can filter this by passing the additional parameters. These can be found in the options section.

## Options
```bash
Options:
  -oc, --oc:      Directs script to run on an OpenShift cluster
  -M, --month:    Displays error occurances by month
  -d, --day:      Displays error occurances by day
  -h, --hour:     Displays error occurances by hour
  -m, --minute:   Displays error occurances by minute
```

## Links
- [Consolidated Article for etcd guidelines with OpenShift Container Platform 4.x](https://access.redhat.com/articles/6967785)
- [omc github](https://github.com/gmeghnag/omc)