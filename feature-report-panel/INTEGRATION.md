# Integrating with our API

Every request must include your API key in the `Authorization` header.

```
Authorization: Bearer YOUR_API_KEY
```

Create and manage keys from your dashboard. A key is shown **only once**, when you create it — store it somewhere safe (a password manager or an environment variable). Never put it in public code, GitHub, or front-end JavaScript that visitors can read.

> Replace `https://api.example.com/v1/ping` below with a real endpoint of our API.

---

## curl

```bash
curl https://api.example.com/v1/ping \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Python

```python
import os
import requests

API_KEY = os.environ["PME_API_KEY"]   # export PME_API_KEY="your-key"

response = requests.get(
    "https://api.example.com/v1/ping",
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=10,
)

print(response.status_code)
print(response.json())
```

Install the library first: `pip install requests`

## JavaScript (Node.js 18+)

```javascript
const API_KEY = process.env.PME_API_KEY; // set it in your environment

const response = await fetch("https://api.example.com/v1/ping", {
  headers: { Authorization: `Bearer ${API_KEY}` },
});

console.log(response.status);
console.log(await response.json());
```

> Call the API from your **server**, not from browser code. Anything in browser JavaScript can be read by your visitors, including your key.

---

## Common responses

| Status | Meaning | What to do |
|--------|---------|------------|
| `200`  | Success | — |
| `401`  | Missing, invalid or revoked key | Check the header; create a new key if needed |
| `429`  | Too many requests (per-minute or daily limit reached) | Wait for the number of seconds in the `Retry-After` header, then retry; check your usage on the dashboard |
| `503`  | Rate limiter temporarily unavailable | Retry shortly |

## Rate limits

Every successful response includes `X-RateLimit-Limit` and `X-RateLimit-Remaining` (requests left today, shared by all your keys). Limits reset at 00:00 UTC.

## Troubleshooting

- **Getting 401?** Make sure there's no extra space or line break in the key, and that the header starts with `Bearer ` (with a space).
- **Revoked a key by mistake?** Revoking can't be undone. Generate a new key and update your app.
- **Lost your key?** It can't be recovered. Generate a new one and revoke the old one.
