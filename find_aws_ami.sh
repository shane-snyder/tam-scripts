query_for_AMIs() {
OWNERS="309956199498 219670896067 531415883065"
for OWNER in $OWNERS
do
  echo "Owner: $OWNER - RHEL-8.2*"
  aws ec2 describe-images --query 'length(Images[].Name)' --owners $OWNER \
    --filter Name=name,Values="RHEL-8.2*"
  echo "Owner: $OWNER - rhcos-410*"
  ## How many can we find (given the filter)
  aws ec2 describe-images --query 'length(Images[].Name)' --owners $OWNER \
    --filters "Name=name,Values=rhcos-410*"
  aws ec2 describe-images --query 'sort_by(Images, &CreationDate)[].Name' \
    --owners $AMI_OWNER --filters "Name=name,Values=rhcos-410*"
done
}

query_for_AMIs
