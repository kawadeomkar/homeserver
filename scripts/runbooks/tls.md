# TLS: the cluster's own CA

Every app answers over HTTPS with a wildcard certificate for `*.<domain>`, which cert-manager signs with the
cluster's own CA and renews 30 days before it expires (`k8s/infrastructure/configs/tls.yaml`). Plain HTTP only
redirects to HTTPS, with HSTS, except for Jellyfin: its apps on TVs and streaming boxes cannot trust a private CA, so
Jellyfin also answers `http://jellyfin.<domain>` (point those apps there).

The CA is a root that its name constraints limit to `<domain>`: its key, if it ever leaked, could vouch for no other
site to your devices. It lives in `k8s/infrastructure/configs/homeserver-ca.sops.yaml`, encrypted for the age key
like every other Secret, so a rebuilt cluster keeps it and your devices keep trusting it. It is valid for ten
years.

## Trusting it on a device, once

Get its certificate, the public half only, from the cluster or from the SOPS file:

```bash
kubectl get secret -n traefik wildcard-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > homeserver-ca.crt
# or: sops -d --extract '["data"]["tls.crt"]' k8s/infrastructure/configs/homeserver-ca.sops.yaml | base64 -d > homeserver-ca.crt
openssl x509 -in homeserver-ca.crt -noout -subject -fingerprint -sha256 -ext nameConstraints
```

Compare the fingerprint on each device as you install it:

- **macOS**: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain homeserver-ca.crt`,
  or open the file, add it to the System keychain and set it to Always Trust.
- **iOS and iPadOS**: AirDrop or mail the file, install the profile (Settings → Profile Downloaded), then turn it on
  under Settings → General → About → Certificate Trust Settings.
- **Android**: Settings → Security → More security settings → Encryption & credentials → Install a certificate →
  CA certificate. Browsers trust it; some apps trust only the system's CAs.
- **Windows**: `certutil -addstore -f Root homeserver-ca.crt` from an administrator's prompt.
- **Linux**: copy it to `/usr/local/share/ca-certificates/homeserver-ca.crt` and run `sudo update-ca-certificates`.
  Firefox reads the system's store with `security.enterprise_roots.enabled`, or import it under its own settings.

The file is public; it needs no protection.

## Making a new CA

For a fork, another domain, or a CA that must be replaced: make one, keyed and signed in memory and handed to sops
(as `k8s/README-secrets.md` makes every Secret), with the domain the cluster uses (`kubernetes_domain`,
`homeserver.internal` by default). Run it in bash, from the repository's root:

```bash
domain=homeserver.internal
file=k8s/infrastructure/configs/homeserver-ca.sops.yaml
key="$(openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256)"
crt="$(openssl req -x509 -new -key <(printf '%s\n' "$key") -days 3650 -sha256 \
  -subj "/O=homeserver/CN=$domain private CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -addext "nameConstraints=critical,permitted;DNS:$domain")"
kubectl create secret tls homeserver-ca -n cert-manager --dry-run=client -o yaml \
  --cert=<(printf '%s\n' "$crt") --key=<(printf '%s\n' "$key") \
  | sops --encrypt --input-type yaml --output-type yaml --filename-override "$file" /dev/stdin > "$file"
unset key crt
```

Commit it. Once Flux applies it, have cert-manager sign the wildcard anew
(`kubectl delete secret -n traefik wildcard-tls`), and trust the new CA on every device in place of the old one.
