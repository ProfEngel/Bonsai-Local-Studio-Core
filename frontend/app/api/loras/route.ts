const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function GET() {
  try {
    const upstream = await fetch(`${BACKEND_URL}/loras`, { cache: "no-store" });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: `Could not reach backend at ${BACKEND_URL}.` }, { status: 502 });
  }
}
