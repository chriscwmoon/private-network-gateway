# Private Network Gateway (PNG)

Reference, architecture, per‑cloud status, and a ready‑to‑run setup notebook for the **Databricks Private Network Gateway (PNG)** — a managed gateway that connects Databricks **serverless compute** to resources in your own cloud network (VNet/VPC) and to networks transitively connected to it (on‑premises over ExpressRoute/VPN/Direct Connect).

> ⚠️ **Preview feature.** PNG is a gated, enterprise preview on both clouds. Status differs per cloud — see [Current status](#current-status-by-cloud). Configuration is **API‑only** (no UI or Terraform yet).

---

## What it is

A private network gateway lets serverless workloads reach private destinations **without onboarding each resource individually**. Instead of a Private Link endpoint per resource, you delegate **one subnet** in your network to Databricks; the gateway is injected there, and serverless traffic to your configured destinations flows through it into your network and onward to anything that network can route to.

It solves three connectivity needs with a single setup:

- **Broad private connectivity** — reach many resources in your VNet/VPC, or on‑prem over ExpressRoute/VPN, without a per‑resource private endpoint.
- **Egress through your own security appliances** — route serverless egress through your firewall (e.g. Palo Alto, a cloud firewall) for inspection before it leaves your network.
- **Stable, identifiable source IPs** — send serverless egress from your own IP range so downstream systems can allowlist it at the network layer.

PNG **complements Private Link**, it does not replace it:
- Use a **Private Link private endpoint** for a direct, private connection to a specific cloud‑managed resource (e.g. object storage).
- Use a **PNG** to reach resources in your network / a connected network, or to route egress through your own firewall.

---

## Current status by cloud

| Cloud | Status | How to enable | Notes |
| --- | --- | --- | --- |
| **Azure** | **Beta** | Account Console → **Previews** page ([manage account‑level previews](https://learn.microsoft.com/en-us/azure/databricks/admin/workspace-settings/manage-previews)) | Enterprise feature. **Not billed during preview** (will be charged later). |
| **AWS** | **Private Preview ("PrPr")** | *"Reach out to your Databricks contact."* Gated by account admin + Enterprise tier + **regional approval** | Available in a **limited set of AWS regions and Availability Zones** during PrPr. |
| **GCP** | Not available | — | Not offered as of this writing. |

---

## Architecture

```
Serverless workload (IPv6-aware lookup + connection)
        │
        ▼
Private Network Gateway  ──►  injected into YOUR delegated subnet
   (IPv6 serverless path, translated to IPv4 on egress)
        │
        ▼
Your VNet / VPC  ──►  transitively connected networks
                      (on-prem via ExpressRoute / VPN / Direct Connect)
```

**Egress path priority.** Databricks evaluates outbound serverless traffic against configured network paths in order and uses the **first match**:

| Priority | Path | Applies to |
| --- | --- | --- |
| 1 | Private Link private endpoint rules | Traffic to a cloud‑managed resource that has a private endpoint rule |
| 2 | Cloud provider endpoints (AWS: S3/DynamoDB gateway endpoints · Azure: service endpoints) | Stays on the provider backbone; **cannot** be overridden by PNG |
| 3 | **Private network gateway** | `SPECIFIC_DESTINATIONS` you configure, or all remaining egress in `ALL_TRAFFIC` mode |
| 4 | Default serverless egress | Everything else |

- **Reuses your existing NCC.** The PNG is created *inside* a [Network Connectivity Config (NCC)](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/) — no new object model. Attach that NCC to your workspaces and serverless products use the gateway automatically.
- **Private endpoint rules always win** — even in `ALL_TRAFFIC` mode, a resource with a private endpoint rule uses that path.
- **Does not cover service‑endpoint cloud services** (e.g. Azure Blob/ADLS via service endpoints) — those keep routing over the provider backbone.

### A note on IPv6 / NAT64 and DNS (observed behavior)
Databricks serverless resolves and connects over **IPv6**, and PNG destinations are **FQDN‑based**; egress is translated to **IPv4** into your network. Practical implications:
- Test resolution from the serverless path with `socket.getaddrinfo(host, port, socket.AF_INET6, …)` — a successful result includes an IPv6 (NAT64, `64:ff9b:…`) address; a `gaierror`/empty result means the serverless path can't obtain an IPv6 destination for that name.
- Backends that hand the client a **bare IPv4 literal** during a redirect (e.g. **Oracle RAC/SCAN** returning a raw node IP) can't be followed — the serverless path needs a **hostname** it can resolve. Ensure such systems redirect by **FQDN** (and add those FQDNs to `destinations`).

---

## Traffic modes

Set by `traffic_mode` at create time:

- **`SPECIFIC_DESTINATIONS`** *(recommended)* — routes only the DNS names in `destinations` through the gateway; all other traffic follows existing routing. In this mode the destinations are **automatically allowed** in serverless egress control. Destination **suffix matching** applies (`mydb.contoso.com` also matches `sub.mydb.contoso.com`).
- **`ALL_TRAFFIC`** — routes **all** serverless egress through the gateway (except more‑specific routes like Private Link). Use when everything must pass through your firewall. You must **explicitly add** all intended destinations to your egress network policy yourself.

---

## Setup

### Azure (Beta)
Prerequisites:
- Service principal that is an **Account Admin** (OAuth app id + secret).
- An existing **NCC**; the NCC, gateway, and subnet must be in the **same region**.
- A **dedicated subnet delegated to `Microsoft.Databricks/workspaces`**, with downstream connectivity to all targets.

Then run the notebook: **[`notebooks/PNG_SETUP_Azure.py`](notebooks/PNG_SETUP_Azure.py)** — a Databricks notebook that walks through **token → create → list → get → patch → delete** via the Account REST API, with `%md` explanations and every parameter documented. *(Credentials in it are obfuscated — replace the `<YOUR_...>` placeholders and pull the SP secret from a secret scope.)*

Create request shape (Azure):
```jsonc
POST /api/2.0/accounts/{account_id}/network-connectivity-configs/{ncc_id}/private-network-gateways
{
  "gateway_name": "my-png-gw",
  "azure_cloud_connection": {
    "gateway_subnet": { "resource_id": "/subscriptions/.../subnets/<delegated-subnet>" }
  },
  "traffic_mode": "SPECIFIC_DESTINATIONS",
  "destinations": [{ "destination_type": "DNS_NAME", "value": "host.internal.example.com" }],
  "private_dns_resolvers": [{ "resolver_type": "IP_ADDRESS", "value": "168.63.129.16" }]  // Azure-provided DNS
}
```

### AWS (Private Preview)
Prerequisites:
- Account admin + **Enterprise** tier; **regionally approved** by Databricks (contact your account team).
- A **dedicated gateway subnet** — minimum **`/28`**, exactly **one subnet in one AZ**, same region/AZ as the NCC, with downstream connectivity.
- A **cross‑account IAM role** whose trust policy allows Databricks (`arn:aws:iam::414351767826:role/png-management-role`) to assume it, with an **external ID matching your Databricks account ID**.
- **Security group(s)** governing gateway egress, and a **DNS resolver** reachable from the subnet (e.g. the VPC resolver `169.254.169.253`, or VPC base +2 such as `10.0.0.2` for `10.0.0.0/16`).

Create request shape (AWS):
```jsonc
POST /api/2.0/accounts/{account_id}/network-connectivity-configs/{ncc_id}/private-network-gateways
{
  "gateway_name": "my-aws-png",
  "traffic_mode": "SPECIFIC_DESTINATIONS",
  "aws_cloud_connection": {
    "gateway_subnets": [{ "subnet_id": "subnet-xxxx" }],
    "cross_account_role": { "role_arn": "arn:aws:iam::<your-account>:role/databricks-png-role" },
    "security_group_ids": ["sg-xxxx"]
  },
  "destinations": [{ "destination_type": "DNS_NAME", "value": "app.customer.internal" }],
  "private_dns_resolvers": [{ "resolver_type": "IP_ADDRESS", "value": "10.0.0.2" }]
}
```

### Gateway lifecycle & states
`CREATING` → **`ESTABLISHED`** (ready; typically < 2 min) → `DELETING`. `FAILED` is terminal — **delete and recreate**. Poll with a GET until `ESTABLISHED` before attaching workloads.

**Patchable in place** (`PATCH` with `update_mask`): `gateway_name`, `traffic_mode`, `destinations`, `private_dns_resolvers`.
**Immutable — delete & recreate to change:** the subnet, cross‑account role, and security groups.

---

## Limitations (preview)

- Configured **only** via the account **REST API** — no UI, no Terraform.
- Gateway and its subnet must be in the **same region as the NCC**.
- Does **not** connect to cloud‑managed services reached via service/gateway endpoints (e.g. Azure ADLS/Blob).
- Supports **serverless** Databricks Runtime products.
- An **NCC supports at most 2 gateways**; a **gateway supports at most 2 DNS resolvers and 100 destinations**.
- AWS: limited regions/AZs during Private Preview; one subnet in one AZ (`/28` minimum).

---

## References

- **Azure — Private network gateway** (Microsoft Learn): <https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/private-network-gateway>
- **AWS — Configure a private network gateway** (Databricks docs): <https://docs.databricks.com/aws/en/security/network/serverless-network-security/private-network-gateway/configure-private-network-gateway>
- **Internal overview deck** (Google Slides): <https://docs.google.com/presentation/d/1jNhWIZKxLGAMGpHYy8jkJbPEL3iKF1R33FbtiCqVTDU/edit>

---

*Status reflects the referenced docs (Azure page updated 2026‑09‑11). Preview features change — confirm current status/regions with your Databricks account team before relying on this for production.*
