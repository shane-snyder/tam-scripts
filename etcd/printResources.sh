CLIENT="oc"
while (( "$#" )); do
  case "$1" in
    -k|--kubectl)
      CLIENT="kubectl"
      shift
      ;;
    -oc|--oc)
      CLIENT="oc"
      shift
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

for api in `$CLIENT api-resources --output=name`; do echo ${api^^} && $CLIENT get $api -A --output name | wc -l; done
