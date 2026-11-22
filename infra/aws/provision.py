"""Create (or resize) BlindSpot's one AWS server: the cheapest setup that runs the whole stack.

Run from services/api (needs only the AWS CLI, signed in with `aws login`):
    cd services/api && uv run python ../../infra/aws/provision.py              # create, or show what exists
    uv run python ../../infra/aws/provision.py --type t4g.micro                # resize (stops ~1 min)
    uv run python ../../infra/aws/provision.py --credits standard              # cap CPU spend
    uv run python ../../infra/aws/provision.py --allow-ssh-here                # your new IP may SSH in

What it makes, in Mumbai (ap-south-1, beside the users and the relay's upstreams), all tagged
Name=blindspot:
- one EC2 instance, Graviton (t4g), Ubuntu 24.04 arm64, 16 GB encrypted gp3 disk. Postgres+PostGIS,
  the API, the website, the relay and Caddy all run on it (infra/aws/setup_server.sh); no RDS, load
  balancer or NAT gateway, each of which would cost more than the server itself;
- an Elastic IP, so the address (and the DNS records) survive a stop or a resize;
- a security group: 80/443 for everyone, 5432 for the data jobs on GitHub's runners (TLS and a
  password only, pg_hba.conf), 22 only from the /24s that ran this script or deploy.py (a VPN such
  as Cloudflare WARP moves the address; logging in still needs the private key).
CPU credits: `standard`, which never bills extra (the CPU slows to the baseline instead).
`unlimited` would bill sustained CPU above the baseline, and AWS free-plan accounts refuse it.
If a resize fails with InsufficientInstanceCapacity, that size is sold out in the server's zone for
now: retry later, or copy the server to another zone (an AMI, as on 2026-11-22: ap-south-1c -> 1b).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REGION = "ap-south-1"
NAME = "blindspot"
DEFAULT_TYPE = "t4g.micro"  # 2 vCPU, 1 GB + 2 GB swap: the smallest that runs the stack
DISK_GB = 16
KEY_PATH = Path.home() / ".ssh" / "blindspot-aws"
AMI_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
TAGS = [{"Key": "Name", "Value": NAME}, {"Key": "project", "Value": NAME}]


def aws_exe() -> str:
    found = shutil.which("aws")
    if found:
        return found
    default = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")
    if default.exists():
        return str(default)
    sys.exit("AWS CLI not found: install it (winget install Amazon.AWSCLI) and run `aws login`.")


def aws(*args: str, check: bool = True) -> dict:
    proc = subprocess.run([aws_exe(), *args, "--region", REGION, "--output", "json"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {proc.stderr.strip()[-800:]}")
    if proc.returncode != 0:
        return {"_error": proc.stderr}
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def tag_spec(kind: str) -> str:
    return json.dumps([{"ResourceType": kind, "Tags": TAGS}])


def my_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as resp:
        return resp.read().decode().strip()


def ensure_key() -> None:
    if not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        keygen = shutil.which("ssh-keygen") or "ssh-keygen"
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "blindspot-aws", "-f", str(KEY_PATH)], check=True)
        print(f"ssh key created: {KEY_PATH}")
    if "_error" in aws("ec2", "describe-key-pairs", "--key-names", NAME, check=False):
        pub = KEY_PATH.with_suffix(".pub").read_text().strip()
        aws("ec2", "import-key-pair", "--key-name", NAME, "--public-key-material",
            __import__("base64").b64encode(pub.encode()).decode(), "--tag-specifications", tag_spec("key-pair"))
        print("key pair imported to AWS (the private key never left this machine)")


def allow(sg: str, port: int, cidr: str, note: str) -> None:
    family = "Ipv6Ranges" if ":" in cidr else "IpRanges"
    key = "CidrIpv6" if ":" in cidr else "CidrIp"
    perm = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port, family: [{key: cidr, "Description": note}]}]
    out = aws("ec2", "authorize-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps(perm), check=False)
    if "_error" in out and "Duplicate" not in out["_error"]:
        raise RuntimeError(out["_error"])


def ensure_security_group() -> str:
    vpcs = aws("ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true")["Vpcs"]
    if not vpcs:
        sys.exit("this region has no default VPC; create one in the VPC console (Actions > Create default VPC)")
    vpc = vpcs[0]["VpcId"]
    groups = aws("ec2", "describe-security-groups", "--filters", f"Name=group-name,Values={NAME}",
                 f"Name=vpc-id,Values={vpc}")["SecurityGroups"]
    if groups:
        return groups[0]["GroupId"]
    sg = aws("ec2", "create-security-group", "--group-name", NAME, "--vpc-id", vpc, "--description",
             "BlindSpot: web, API, database for the jobs", "--tag-specifications", tag_spec("security-group"))["GroupId"]
    for port in (80, 443):
        allow(sg, port, "0.0.0.0/0", "web")
        allow(sg, port, "::/0", "web")
    allow(sg, 5432, "0.0.0.0/0", "data jobs on GitHub runners (TLS + password)")
    allow(sg, 22, f"{my_ip()}/32", "ssh from the deployer")
    print(f"security group created: {sg}")
    return sg


def find_instance() -> dict | None:
    res = aws("ec2", "describe-instances", "--filters", f"Name=tag:Name,Values={NAME}",
              "Name=instance-state-name,Values=pending,running,stopping,stopped")["Reservations"]
    instances = [i for r in res for i in r["Instances"]]
    return instances[0] if instances else None


def create_instance(sg: str, itype: str) -> dict:
    ami = aws("ssm", "get-parameter", "--name", AMI_PARAM)["Parameter"]["Value"]
    disks = [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": DISK_GB, "VolumeType": "gp3",
                                                  "DeleteOnTermination": True, "Encrypted": True}}]
    out = aws("ec2", "run-instances", "--image-id", ami, "--instance-type", itype, "--key-name", NAME,
              "--security-group-ids", sg, "--count", "1",
              "--block-device-mappings", json.dumps(disks),
              "--credit-specification", "CpuCredits=standard",
              "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled",
              "--tag-specifications", json.dumps([{"ResourceType": kind, "Tags": TAGS} for kind in ("instance", "volume")]))
    inst = out["Instances"][0]
    print(f"instance launched: {inst['InstanceId']} ({itype}, {ami})")
    return inst


def ensure_eip(instance_id: str) -> str:
    addrs = aws("ec2", "describe-addresses", "--filters", f"Name=tag:Name,Values={NAME}")["Addresses"]
    if addrs:
        addr = addrs[0]
    else:
        addr = aws("ec2", "allocate-address", "--domain", "vpc", "--tag-specifications", tag_spec("elastic-ip"))
        print(f"elastic IP allocated: {addr['PublicIp']}")
    if addr.get("InstanceId") != instance_id:
        aws("ec2", "wait", "instance-running", "--instance-ids", instance_id)
        aws("ec2", "associate-address", "--instance-id", instance_id, "--allocation-id", addr["AllocationId"])
    return addr["PublicIp"]


def resize(inst: dict, itype: str) -> None:
    iid = inst["InstanceId"]
    if inst["InstanceType"] == itype:
        print(f"already {itype}")
        return
    print(f"resizing {iid}: {inst['InstanceType']} -> {itype} (the site is down for about a minute)")
    aws("ec2", "stop-instances", "--instance-ids", iid)
    aws("ec2", "wait", "instance-stopped", "--instance-ids", iid)
    aws("ec2", "modify-instance-attribute", "--instance-id", iid, "--instance-type", json.dumps({"Value": itype}))
    aws("ec2", "start-instances", "--instance-ids", iid)
    aws("ec2", "wait", "instance-running", "--instance-ids", iid)
    print("running again")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--type", help=f"instance type to create with (default {DEFAULT_TYPE}) or resize to")
    parser.add_argument("--credits", choices=["standard", "unlimited"], help="CPU credit mode")
    parser.add_argument("--allow-ssh-here", action="store_true", help="let this machine's current IP use SSH")
    args = parser.parse_args()

    who = aws("sts", "get-caller-identity")
    print(f"AWS account {who['Account']}, region {REGION}")
    ensure_key()
    sg = ensure_security_group()
    if args.allow_ssh_here:
        allow(sg, 22, f"{my_ip()}/32", "ssh from the deployer")
        print("ssh allowed from this IP")
    inst = find_instance()
    if inst is None:
        inst = create_instance(sg, args.type or DEFAULT_TYPE)
    elif args.type:
        resize(inst, args.type)
    iid = inst["InstanceId"]
    ip = ensure_eip(iid)
    if args.credits:
        aws("ec2", "modify-instance-credit-specification", "--instance-credit-specification",
            json.dumps([{"InstanceId": iid, "CpuCredits": args.credits}]))
        print(f"CPU credits: {args.credits}")
    inst = find_instance() or inst
    credits = aws("ec2", "describe-instance-credit-specifications", "--instance-ids", iid)
    mode = credits["InstanceCreditSpecifications"][0]["CpuCredits"]
    print(json.dumps({"instance": iid, "type": inst["InstanceType"], "state": inst["State"]["Name"],
                      "ip": ip, "cpu_credits": mode, "ssh": f"ssh -i {KEY_PATH} ubuntu@{ip}"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
