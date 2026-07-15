const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

export async function POST(request: Request) {
  const payload = await request.text();
  try {
    const upstream = await fetch(`${BACKEND_URL}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      cache: "no-store",
    });
    if (!upstream.body) {
      return Response.json({ detail: "Der lokale Fortschritts-Stream lieferte keinen Inhalt." }, { status: 502 });
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "text/event-stream",
        "Cache-Control": "no-cache",
      },
    });
  } catch {
    return Response.json({ detail: `Could not reach backend at ${BACKEND_URL}.` }, { status: 502 });
  }
}
