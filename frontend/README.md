# PolyEdge Control Plane Frontend

Next.js dashboard for the paper-first PolyEdge backend.

```bash
cp .env.example .env.local
npm install
npm run dev
```

The browser calls same-origin Next.js routes. `BACKEND_API_BEARER_TOKEN` is only read by the Next.js server proxy and is not stored in browser localStorage.
