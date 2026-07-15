const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

async function proxy(method: "GET" | "PUT", request?: Request) {
  try {
    const upstream = await fetch(`${BACKEND_URL}/studio-settings`, {
      method,
      headers: method === "PUT" ? { "Content-Type": "application/json" } : undefined,
      body: request ? await request.text() : undefined,
      cache: "no-store",
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: "Lokale Studio-Einstellungen konnten nicht erreicht werden." }, { status: 502 });
  }
}

export function GET() {
  return proxy("GET");
}

export function PUT(request: Request) {
  return proxy("PUT", request);
}
