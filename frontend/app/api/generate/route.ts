const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";
export const maxDuration = 600; // seconds

export async function POST(request: Request) {
  const payload = await request.text();

  try {
    JSON.parse(payload);
  } catch {
    return Response.json({ detail: "Invalid JSON body." }, { status: 400 });
  }

  try {
    const upstream = await fetch(`${BACKEND_URL}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      cache: "no-store",
    });
    const contentType = upstream.headers.get("content-type") ?? "application/octet-stream";
    const body = await upstream.arrayBuffer();
    const headers: Record<string, string> = { "Content-Type": contentType };
    const peak = upstream.headers.get("x-peak-memory-mb");
    const wall = upstream.headers.get("x-wall-seconds");
    if (peak) headers["X-Peak-Memory-MB"] = peak;
    if (wall) headers["X-Wall-Seconds"] = wall;
    return new Response(body, { status: upstream.status, headers });
  } catch {
    return Response.json(
      { detail: `Could not reach backend at ${BACKEND_URL}.` },
      { status: 502 },
    );
  }
}
