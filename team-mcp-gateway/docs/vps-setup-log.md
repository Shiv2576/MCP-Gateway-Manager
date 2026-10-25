# VPS setup log (2026-10-15)

How the `store-check` cluster was copied onto the VPS, step by step. It follows
[deploy-vps.md](deploy-vps.md); the differences are this server's, listed first.

## The server

| | |
|---|---|
| SSH | `ssh -i ~/.ssh/id_ed25519_work -p 20045 root@147.135.130.116` (or port 22 on the IPv6) |
| OS | Ubuntu 24.04, x86_64, 2 vCPU, 8 GB RAM, 80 GB disk |
| Kind | **LXC container** on Proxmox (`systemd-detect-virt` → `lxc`) |
| IPv4 | **shared NAT** `147.135.130.116`: only SSH (20045 → 22) is forwarded; 80/443 belong to the provider's Caddy |
| IPv6 | **dedicated** `2001:41d0:303:2774::94`, all ports reach the server |
| Private IPv4 | `10.10.10.48` (k3s node IP; outbound IPv4 is NATed through 147.135.130.116) |

So the cluster is public **over IPv6 only**:

`BASE = 2001-41d0-303-2774--94.sslip.io` (sslip.io answers AAAA for dashed IPv6 names, no A record).

| URL | What |
|---|---|
| `https://dash.BASE` | dashboard |
| `https://gateway.BASE` | gateway API |
| `https://store.BASE` | the `mercora` store |
| `https://<store>.BASE` | stores created from the dashboard |
| `https://SLUG.gw.BASE/mcp` | MCP endpoint for Claude |

## What differs from deploy-vps.md

Four additions, all because of LXC + IPv6-only. Everything else is the doc as written.

1. **k3s inside LXC.** The container has no `/dev/kmsg` and can't write
   conntrack sysctls, so kubelet and kube-proxy crash-loop on a plain install.
   Fix (before or after `get.k3s.io`, then `systemctl restart k3s`):

   ```
   echo "L /dev/kmsg - - - - /dev/console" > /etc/tmpfiles.d/kmsg.conf
   systemd-tmpfiles --create /etc/tmpfiles.d/kmsg.conf
   mkdir -p /etc/rancher/k3s && cat > /etc/rancher/k3s/config.yaml <<EOF
   # LXC container: no /dev/kmsg, no access to conntrack sysctls.
   kubelet-arg:
     - feature-gates=KubeletInUserNamespace=true
   kube-proxy-arg:
     - conntrack-max-per-core=0
   EOF
   ```

   The `modprobe br_netfilter/overlay` failures in `systemctl status k3s` are
   harmless: the Proxmox host already has those modules loaded.

2. **IPv6 in → Traefik.** k3s is IPv4-only and its ServiceLB (Traefik) listens
   on the node's IPv4 `10.10.10.48`. Two socat units forward the public IPv6:

   ```
   apt-get install -y socat
   for p in 80 443; do cat > /etc/systemd/system/ipv6-fwd-$p.service <<EOF
   [Unit]
   Description=Forward IPv6 :$p to Traefik (k3s ServiceLB on the node IPv4)
   After=k3s.service
   [Service]
   ExecStart=/usr/bin/socat TCP6-LISTEN:$p,ipv6only=1,reuseaddr,fork TCP4:10.10.10.48:$p
   Restart=always
   [Install]
   WantedBy=multi-user.target
   EOF
   done
   systemctl daemon-reload && systemctl enable --now ipv6-fwd-80 ipv6-fwd-443
   ```

   Side effect: every request reaches Traefik from `10.10.10.48`, so the real
   client IP is lost (logs and any per-IP limits see one address).

3. **`*.BASE` inside the cluster.** Pods are IPv4-only and the sslip.io names
   have only AAAA records, so cert-manager's HTTP-01 self-check (and anything
   in a pod calling a public URL, e.g. WordPress calling itself) would fail.
   CoreDNS answers A records for `BASE` with Traefik's ClusterIP:

   ```
   B=2001-41d0-303-2774--94.sslip.io
   IP=$(kubectl -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}')
   kubectl apply -f - <<EOF
   apiVersion: v1
   kind: ConfigMap
   metadata: { name: coredns-custom, namespace: kube-system }
   data:
     base.server: |
       $B:53 {
         template IN A $B {
           answer "{{ .Name }} 60 IN A $IP"
         }
         template IN AAAA $B {
           rcode NOERROR
         }
       }
   EOF
   kubectl -n kube-system rollout restart deploy coredns
   ```

   Each `template` block must span several lines; on one line CoreDNS
   crash-loops with `Wrong argument count`.

4. **Legacy iptables.** With Ubuntu's default nftables backend, kube-router's
   network-policy sync fails inside LXC
   (`iptables-restore: line 440 failed: Message too long` in `journalctl -u k3s`).
   The policies exist but their allow rules are never written, so e.g. the
   gateway can't reach Postgres (`cannot reach the database within 10s`,
   CrashLoopBackOff). Switch to legacy iptables (the host has `ip_tables` loaded):

   ```
   /usr/local/bin/k3s-killall.sh
   update-alternatives --set iptables /usr/sbin/iptables-legacy
   update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
   nft flush ruleset
   ufw --force disable && ufw --force enable
   systemctl start k3s
   ```

   Best done right after installing k3s, before anything else. Check: every
   `KUBE-NWPLCY-*` chain has ACCEPT rules
   (`for c in $(iptables -S | grep -oE "KUBE-NWPLCY-[A-Z0-9]{16}" | sort -u); do iptables -S $c | grep -c ACCEPT; done`).

Also: SSH comes in on port 22 inside the container (NAT 20045 → 22), so the
`ufw allow 22/tcp` from the doc is the right rule.

## Steps run, in order

1. **Laptop:** `sed -i '' 's/example\.com/2001-41d0-303-2774--94.sslip.io/g' deploy/*-prod.yaml`;
   in `deploy/tls-prod.yaml` set the email (`shivangdixit004@gmail.com`) and
   add `store.BASE` to `dnsNames`.
2. **Laptop:** `ssh-copy-id -p 20045 root@147.135.130.116` (installs `id_ed25519_work`).
3. **VPS:** `apt upgrade`, `apt install curl socat ufw`, ufw rules (22, 80, 443,
   `10.42.0.0/16`, `10.43.0.0/16`), k3s, helm, `KUBECONFIG` in `.bashrc`.
4. **VPS:** LXC fix (above), restart k3s; node Ready on `v1.36.5+k3s1`.
5. **VPS:** cert-manager (jetstack chart, `crds.enabled=true`).
6. **VPS:** IPv6 forwarders (above). `curl http://[2001:41d0:303:2774::94]/`
   from the laptop → 404 from Traefik.
7. **VPS:** CoreDNS override (above). `nslookup dash.BASE` in a pod → Traefik ClusterIP.
8. **Laptop:** amd64 images built as in deploy-vps.md step 3 (gateway 0.7.3 +
   0.6.1, dashboard 0.7.3, woo-mcp 0.5.0, demo-mcp 0.1.0, store-operator 0.5.3),
   repo rsynced to `/opt/team-mcp-gateway` (without `.git`, `node_modules`,
   `.venv`), tarballs imported with `k3s ctr images import`.
9. **VPS:** `kubectl apply -f deploy/tls-prod.yaml`; certificate `wildcard`
   Ready in 37 s (HTTP-01 over IPv6 works).
10. **VPS:** namespace `mercora` and Secrets, exactly as deploy-vps.md step 5
    (new random values; no `OPENROUTER_API_KEY` yet).
11. **VPS:** releases `db`, `mercora`, `woo-mcp`, `notes`, `fx`, `team`
    (deploy-vps.md step 6). `mercora` setup: "store setup done". `team`
    crash-looped on the database: fixed by the legacy-iptables switch (4 above),
    then `team-ui` installed. All pods Running.
12. **Browser:** first-run setup at `https://dash.BASE` with the `SETUP_TOKEN`
    (org + admin), then API keys → admin key.
13. **VPS:** `store-operator-gateway` Secret with that key, then
    `helm upgrade --install store-operator ... -f deploy/store-operator-values-prod.yaml`.
    CRDs (`stores`, `mcpgateways`, `hostedservers`) and the
    `mercora-store-operator-own-namespaces` admission policy are in place; the
    operator reaches the gateway. One `429 storage is (re)initializing` right
    after the CRDs are created is harmless (kopf retries).

14. **Dashboard:** gateway `prod` (Ready, certificate in 40 s) and store `shop1`.
    `shop1`'s certificate stuck at `wrong status code '502'`: the store
    namespace's `default-deny` blocked Traefik from cert-manager's HTTP-01
    solver pod. **Operator fix:** `network_policies()` in
    `services/store-operator/store_operator/manifests.py` now adds
    `acme-solver-ingress` (kube-system → solver pods, TCP 8089). Rebuilt
    `store-operator:0.5.3`, imported, restarted; applied the same policy by hand
    to `store-shop1`, certificate issued. Never seen on k3d (no cert-manager).

Next (deploy-vps.md step 7): create stores and a gateway `SLUG` with public URL
`https://SLUG.gw.2001-41d0-303-2774--94.sslip.io`, then add
`https://SLUG.gw.2001-41d0-303-2774--94.sslip.io/mcp` as a Claude connector.

## Known issue: IPv6 reachability is flaky

From the laptop (a mobile ISP's IPv6), about half of new TCP connections to the
server time out, **on port 22 too** (plain sshd, no socat or Traefik), and
ICMPv6 pings never reach the server's firewall (the ufw counter doesn't move).
The drop is upstream of the container: the provider's Proxmox/OVH firewall or the
route. Not caused by this setup. If it persists from other networks, raise it
with the provider; a retry usually connects.

## Problems adding the gateway as a Claude connector (OAuth, 2026-10-16)

Adding `https://prod.gw.2001-41d0-303-2774--94.sslip.io/mcp` as a custom
connector in Claude stopped at step 2 with *"No server responded at this URL"*.
Not fixed yet. Checks from the laptop:

1. **The gateway itself is up.** `POST /mcp` with an `initialize` body returns
   `401 {"error":"a valid API key or OAuth token is required"}` with
   `www-authenticate: Bearer`, which is the right answer without a token.
2. **The hostname is IPv6-only.** sslip.io gives an AAAA record
   (`2001:41d0:303:2774::94`) and no A record; `curl -4` can't resolve it.
   Claude probes connectors from Anthropic's servers, not the browser, so if
   that probe goes over IPv4 it finds no address. Most likely cause, not
   confirmed from Anthropic's side.
3. **Inbound IPv6 drops connections** (see the known issue above): one of two
   requests to `/.well-known/oauth-protected-resource` timed out after 20 s.
   Even an IPv6-capable probe can fail at random.
4. **`/.well-known/oauth-authorization-server` at the root returns 404.**
   Untested whether Claude finds the auth server through the
   protected-resource metadata instead; check this once (2) and (3) are fixed.

Don't pick "Continue anyway": sign-in and every tool call need the same
reachability, so they would fail too.

**Planned fix:** expose the gateway over IPv4 with a Cloudflare Tunnel
(`cloudflared` makes outbound connections only, so neither the NAT nor the flaky
inbound IPv6 matters), or have the provider's shared-IPv4 Caddy forward the
gateway host to the box. Then set the gateway's public URL / OAuth issuer to the
new hostname, add `https://<new-host>/mcp` in Claude, and choose
"Register automatically" (DCR) as the OAuth client.

## Claude connector over IPv4: Tailscale Funnel (2026-10-16)

Claude's connector reaches servers from Anthropic's side, and the sslip.io
names here are IPv6-only (and inbound IPv6 is flaky), so "No server responded".
The VPS has no public IPv4 to listen on, so the gateway edge alone is published
through Tailscale Funnel: the VPS dials out to Tailscale, and Tailscale's
relays serve `https://mcp-gateway.tailff0a64.ts.net` over IPv4 and IPv6 with a
real certificate. Dashboard and stores stay on their IPv6 URLs.

```
curl -fsSL https://tailscale.com/install.sh | sh
# LXC has no /dev/net/tun: run tailscaled in userspace mode (Funnel works in it).
sed -i 's/^FLAGS=.*/FLAGS="--tun=userspace-networking"/' /etc/default/tailscaled
systemctl reset-failed tailscaled; systemctl restart tailscaled
tailscale up --hostname=mcp-gateway          # open the printed login URL
# Admin console: DNS → Enable HTTPS; enable Funnel (the funnel command prints the link if missing).
tailscale funnel --bg --yes https+insecure://10.10.10.48:443
tailscale funnel status
```

- Funnel forwards to Traefik over https (`+insecure`: Traefik's default
  certificate doesn't name the ts.net host; http would hit the global https
  redirect). Traefik routes by Host to the edge.
- In the dashboard: Gateways → `prod` → public URL
  `https://mcp-gateway.tailff0a64.ts.net`. The operator adds that host to the
  edge's Ingress (the sslip host stays). OAuth links are built from it.
- Claude connector: `https://mcp-gateway.tailff0a64.ts.net/mcp`.
- A new Funnel name took ~6 minutes to appear in public DNS.
- Funnel gives one hostname per machine (ports 443, 8443, 10000 only), so it
  carries just the edge. Off: `tailscale funnel --https=443 off`.
- Userspace mode: the VPS can't reach other tailnet machines; not needed here.
- A gateway's public URL can't be edited (the API returns 409 for a changed
  URL; the dashboard only creates). `prod` was deleted and recreated through
  the API with the new URL, then `shop1`/`shop2` re-attached:
  `DELETE /api/gateways/prod`, `POST /api/gateways`
  `{"slug":"prod","name":"Production","public_url":"https://mcp-gateway.tailff0a64.ts.net"}`,
  `PUT /api/gateways/prod/servers {"server_ids":[1,3]}`.
  Check over IPv4: `/mcp` → 401 with `resource_metadata=.../.well-known/oauth-protected-resource/mcp` (200),
  `/.well-known/oauth-authorization-server` → 200.

## Secrets encrypted at rest (2026-10-25)

k3s's built-in encryption, one AES-CBC key for every namespace (the API
server picks keys per resource type, not per namespace).

```sh
# backup taken with k3s stopped (db/ + config.yaml), deleted after the check: it held plaintext Secrets
echo 'secrets-encryption: true' >> /etc/rancher/k3s/config.yaml
systemctl restart k3s
k3s secrets-encrypt reencrypt -f      # rewrites the existing Secrets
k3s secrets-encrypt status            # Enabled, reencrypt_finished
```

- Key: `/var/lib/rancher/k3s/server/cred/encryption-config.json`. Keep a copy
  off the box; without it the Secrets in `state.db` can't be read.
- Checked in `state.db` (kine table): 94/94 live Secrets start with `k8s:enc:aescbc:v1:`.
- Rotate: `k3s secrets-encrypt rotate-keys` (adds the new key, switches writes, re-encrypts, drops the old one).
- Not in GitOps: it is node config, so Argo CD doesn't manage it.

### Switched aescbc → secretbox (2026-10-25)

secretbox (XSalsa20-Poly1305) also detects tampering: a changed row fails to
decrypt instead of decrypting to garbage. AES-CBC has no such check.

```sh
echo 'secrets-encryption-provider: secretbox' >> /etc/rancher/k3s/config.yaml
systemctl restart k3s                 # still aescbc: the provider only applies to new keys
k3s secrets-encrypt rotate-keys       # new secretbox key first, re-encrypt all, old aescbc key dropped
```

- Status: one key, `XSalsa20-POLY1305 secretboxkey-2026-10-25T09:11:42Z`.
- Checked in `state.db`: 94/94 Secrets start with `k8s:enc:secretbox:v1:`; pods Running, gateway `/readyz` 200.
- k3s marks `--secrets-encryption-provider` experimental (v1.36.5).
