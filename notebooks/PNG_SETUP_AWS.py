# Databricks notebook source
# MAGIC %md
# MAGIC # AWS — Private Network Gateway (PNG) Setup
# MAGIC
# MAGIC Create and manage an **AWS Databricks Private Network Gateway** through the **Account REST API**.
# MAGIC A PNG connects Databricks **serverless compute** to resources in your VPC (and networks
# MAGIC transitively connected to it — on‑prem over Direct Connect/VPN) through a single managed gateway
# MAGIC placed into a subnet you own, reached via a **cross‑account IAM role**.
# MAGIC
# MAGIC > **Status: Beta.** Enable it in the Account Console → **Previews** page. Not billed during preview.
# MAGIC > Configured **only** via the account REST API (no UI/Terraform yet).
# MAGIC
# MAGIC ### Prerequisites
# MAGIC - A **service principal that is an Account Admin** (OAuth app id + secret).
# MAGIC - An existing **Network Connectivity Config (NCC)** — the PNG is created *inside* it. The NCC,
# MAGIC   the gateway, and its subnet must all be in the **same region**.
# MAGIC - A **subnet** in your VPC where the gateway network interface is placed (Private Preview accepts
# MAGIC   **exactly one** subnet), a **cross‑account IAM role** Databricks assumes, and **security groups**
# MAGIC   that control the gateway's egress.
# MAGIC
# MAGIC ### ⚠️ Credentials are obfuscated
# MAGIC Every `<YOUR_...>` placeholder below must be replaced with your own value. The service‑principal
# MAGIC secret is pulled from a **secret scope** — do **not** hard‑code it in the notebook.
# MAGIC
# MAGIC ### Limits (preview)
# MAGIC An NCC supports at most **2 gateways**; a gateway supports at most **2 DNS resolvers** and **100 destinations**.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Get an ACCESS_TOKEN (Service Principal with Account Admin access)
# MAGIC
# MAGIC Exchanges the service principal's `client_id` + `client_secret` for a short‑lived account‑level
# MAGIC OAuth token (the non‑interactive `client_credentials` grant). Reuse the returned `token` as the
# MAGIC `Bearer` for every call below (valid ~1 hour).
# MAGIC
# MAGIC **Parameters to provide**
# MAGIC | Variable | What it is |
# MAGIC | --- | --- |
# MAGIC | `HOST` | Account console host. AWS: `https://accounts.cloud.databricks.com` |
# MAGIC | `DATABRICKS_ACCOUNT_ID` | Your Databricks **account** ID (Account Console → top‑right) |
# MAGIC | `SP_CLIENT_ID` | The service principal's OAuth **application (client) ID** |
# MAGIC | `SP_SECRET` | The SP's OAuth **secret** — stored in a **secret scope**, never inline |

# COMMAND ----------

HOST = "https://accounts.cloud.databricks.com"
DATABRICKS_ACCOUNT_ID = "<YOUR_DATABRICKS_ACCOUNT_ID>"          # 👈 e.g. 407ca2f1-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SP_CLIENT_ID = "<YOUR_SP_CLIENT_ID>"                            # 👈 service principal OAuth app id
# 👇 store the SP secret in a secret scope: dbutils.secrets.get("<scope>", "<key>")
SP_SECRET = dbutils.secrets.get("png", "sp-secret")             # 👈 do NOT hard-code the secret

# COMMAND ----------

import os
import json
import requests

url = f"{HOST}/oidc/accounts/{DATABRICKS_ACCOUNT_ID}/v1/token"

resp = requests.post(
    url,
    data={
        "grant_type": "client_credentials",
        "client_id": SP_CLIENT_ID,
        "client_secret": SP_SECRET,
        "scope": "all-apis",
    },
    timeout=30,
)
resp.raise_for_status()                       # fail loudly if auth is rejected
token = resp.json()["access_token"]

print("token acquired, length:", len(token))  # don't print the token itself

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — CREATE the Private Network Gateway
# MAGIC
# MAGIC Creates the gateway inside an existing NCC. Creation is **asynchronous**: the call returns with
# MAGIC `state = CREATING`; the gateway becomes usable once it reaches **`ESTABLISHED`** (typically < 2 min —
# MAGIC poll with Step 3).
# MAGIC
# MAGIC **Parameters to provide**
# MAGIC | Variable | What it is |
# MAGIC | --- | --- |
# MAGIC | `NCC_ID` | The Network Connectivity Config the gateway lives in (same region as the subnet) |
# MAGIC | `GATEWAY_NAME` | Any human‑readable name |
# MAGIC | `GATEWAY_SUBNETS` | The subnet where the gateway network interface is placed. Private Preview accepts **exactly one** subnet |
# MAGIC | `CROSS_ACCOUNT_ROLE` | ARN of the IAM role Databricks assumes (created in the previous setup step) |
# MAGIC | `SECURITY_GROUP_IDS` | Security groups attached to the gateway network interface — they control egress |
# MAGIC | `TRAFFIC_MODE` | `SPECIFIC_DESTINATIONS` (recommended) or `ALL_TRAFFIC` |
# MAGIC | `DESTINATIONS` | For `SPECIFIC_DESTINATIONS`: the DNS names to route (suffix matching applies — `mydb.example.com` also matches `sub.mydb.example.com`) |
# MAGIC | `PRIVATE_DNS_RESOLVERS` | DNS resolver(s) reachable from the subnet. AWS built‑in VPC resolver = `169.254.169.253` |
# MAGIC
# MAGIC > In `SPECIFIC_DESTINATIONS` mode, the destinations are **auto‑allowed** in serverless egress control.
# MAGIC > In `ALL_TRAFFIC` mode you must add them to your egress network policy yourself.

# COMMAND ----------

NCC_ID = "<YOUR_NCC_ID>"                                        # 👈 e.g. 57c7ef49-xxxx-...

# --- PNG config parameters ---
GATEWAY_NAME = "my-png-gw"                                      # 👈 name for the gateway
GATEWAY_SUBNETS = [{"subnet_id": "<YOUR_SUBNET_ID>"}]           # 👈 exactly one subnet (e.g. subnet-xxxxxxxx)
CROSS_ACCOUNT_ROLE = "arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/<YOUR_PNG_ROLE>"  # 👈 IAM role ARN
SECURITY_GROUP_IDS = ["<YOUR_SECURITY_GROUP_ID>"]              # 👈 e.g. sg-xxxxxxxx
TRAFFIC_MODE = "SPECIFIC_DESTINATIONS"                         # 👈 SPECIFIC_DESTINATIONS or ALL_TRAFFIC
DESTINATIONS = [                                               # 👈 only used for SPECIFIC_DESTINATIONS
    {"destination_type": "DNS_NAME", "value": "<host1.internal.example.com>"},
    {"destination_type": "DNS_NAME", "value": "<host2.example.com>"},
]
PRIVATE_DNS_RESOLVERS = [{"resolver_type": "IP_ADDRESS", "value": "169.254.169.253"}]  # 👈 AWS built-in VPC resolver

url = (
    f"{HOST}/api/2.0/accounts/{DATABRICKS_ACCOUNT_ID}"
    f"/network-connectivity-configs/{NCC_ID}/private-network-gateways"
)

resp = requests.post(
    url,
    headers={"Authorization": f"Bearer {token}"},
    json={
        "gateway_name": GATEWAY_NAME,
        "aws_cloud_connection": {
            "gateway_subnets": GATEWAY_SUBNETS,
            "cross_account_role": {"role_arn": CROSS_ACCOUNT_ROLE},
            "security_group_ids": SECURITY_GROUP_IDS,
        },
        "traffic_mode": TRAFFIC_MODE,
        "destinations": DESTINATIONS,
        "private_dns_resolvers": PRIVATE_DNS_RESOLVERS,
    },
    timeout=60,
)
print(resp.status_code)
print(json.dumps(resp.json(), indent=2))       # note the returned gateway_id and state (CREATING)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — GET all PNGs in the NCC (and wait for `ESTABLISHED`)
# MAGIC
# MAGIC Lists every gateway in the NCC. Use it to find your `gateway_id` and watch `state` move from
# MAGIC `CREATING` → **`ESTABLISHED`** (ready). A `FAILED` state is terminal — delete and recreate.
# MAGIC
# MAGIC **Parameters to provide:** `NCC_ID`.

# COMMAND ----------

NCC_ID = "<YOUR_NCC_ID>"                                        # 👈

url = (
    f"{HOST}/api/2.0/accounts/{DATABRICKS_ACCOUNT_ID}"
    f"/network-connectivity-configs/{NCC_ID}"
    "/private-network-gateways"
)

resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
print(resp.status_code)
print(json.dumps(resp.json(), indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — GET a specific PNG
# MAGIC
# MAGIC Retrieves one gateway by id — use it to poll a single gateway's `state`/config.
# MAGIC
# MAGIC **Parameters to provide:** `NCC_ID`, `GATEWAY_ID` (from Step 2's response or Step 3's list).

# COMMAND ----------

NCC_ID = "<YOUR_NCC_ID>"                                        # 👈
GATEWAY_ID = "<YOUR_GATEWAY_ID>"                                # 👈 from the create/list response

url = (
    f"{HOST}/api/2.0/accounts/{DATABRICKS_ACCOUNT_ID}"
    f"/network-connectivity-configs/{NCC_ID}"
    f"/private-network-gateways/{GATEWAY_ID}"
)

resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
print(resp.status_code)
print(json.dumps(resp.json(), indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — PATCH the PNG config
# MAGIC
# MAGIC Updates a gateway in place. Only `gateway_name`, `traffic_mode`, `destinations`, and
# MAGIC `private_dns_resolvers` are patchable — list what you change in `update_mask`. To change the
# MAGIC **subnet**, **cross‑account role**, or **security groups**, you must **delete and recreate**.
# MAGIC
# MAGIC **Parameters to provide:** `NCC_ID`, `GATEWAY_ID`, and the new `destinations` / `private_dns_resolvers` / `traffic_mode`.

# COMMAND ----------

NCC_ID = "<YOUR_NCC_ID>"                                        # 👈
GATEWAY_ID = "<YOUR_GATEWAY_ID>"                                # 👈

url = (
    f"{HOST}/api/2.0/accounts/{DATABRICKS_ACCOUNT_ID}"
    f"/network-connectivity-configs/{NCC_ID}"
    f"/private-network-gateways/{GATEWAY_ID}"
)

resp = requests.patch(
    url,
    params={"update_mask": "traffic_mode,destinations,private_dns_resolvers"},
    headers={"Authorization": f"Bearer {token}"},
    json={
        "traffic_mode": "SPECIFIC_DESTINATIONS",
        "destinations": [
            {"destination_type": "DNS_NAME", "value": "<host1.internal.example.com>"},
            {"destination_type": "DNS_NAME", "value": "<host2.example.com>"},
        ],
        "private_dns_resolvers": [
            {"resolver_type": "IP_ADDRESS", "value": "169.254.169.253"},
        ],
    },
    timeout=60,
)
print(resp.status_code)
print(json.dumps(resp.json(), indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — DELETE the PNG
# MAGIC
# MAGIC Removes the gateway. Also required before recreating a `FAILED` gateway or changing its
# MAGIC immutable fields (subnet / cross‑account role / security groups).
# MAGIC
# MAGIC **Parameters to provide:** `NCC_ID`, `GATEWAY_ID`.

# COMMAND ----------

NCC_ID = "<YOUR_NCC_ID>"                                        # 👈
GATEWAY_ID = "<YOUR_GATEWAY_ID>"                                # 👈

url = (
    f"{HOST}/api/2.0/accounts/{DATABRICKS_ACCOUNT_ID}"
    f"/network-connectivity-configs/{NCC_ID}"
    f"/private-network-gateways/{GATEWAY_ID}"
)

resp = requests.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
print(resp.status_code)
print(json.dumps(resp.json(), indent=2))
