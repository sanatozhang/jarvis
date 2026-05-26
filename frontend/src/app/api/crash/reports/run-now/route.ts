/**
 * Custom proxy for /api/crash/reports/run-now —— Next.js 默认 rewrites 在 30s 切
 * ECONNRESET，但 compose_report 实测 35-60s（dry_run preview 也要拉 Datadog 多次）。
 *
 * 此 route handler 优先级高于 next.config.ts 的 rewrites，专门给这个长接口设 120s
 * AbortSignal，避免 socket hang up 误报 500。
 *
 * 治本方案见 docs/modules/crashguard.md「报告异步化」TODO；此处先做最小颗粒度修复。
 */
import { NextRequest } from "next/server";

export const maxDuration = 120; // Next.js / Vercel serverless 单 route 最大超时（秒）
export const dynamic = "force-dynamic";

const BACKEND_BASE = process.env.NEXT_PUBLIC_API_URL || "http://backend:8000";
const TIMEOUT_MS = 120_000;

export async function POST(req: NextRequest): Promise<Response> {
  const body = await req.text();
  const upstream = `${BACKEND_BASE}/api/crash/reports/run-now`;

  try {
    const resp = await fetch(upstream, {
      method: "POST",
      headers: {
        "Content-Type": req.headers.get("content-type") || "application/json",
        // 透传 cookie 用于 SSO（ENABLE_SSO=true 时 backend middleware 需要）
        ...(req.headers.get("cookie") ? { cookie: req.headers.get("cookie")! } : {}),
      },
      body,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers: {
        "content-type": resp.headers.get("content-type") || "application/json",
      },
    });
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    const isTimeout = msg.includes("aborted") || msg.includes("timeout");
    return new Response(
      JSON.stringify({
        ok: false,
        error: isTimeout
          ? `backend timeout (>${TIMEOUT_MS / 1000}s) — try again or check backend logs`
          : `proxy failed: ${msg}`,
      }),
      {
        status: isTimeout ? 504 : 502,
        headers: { "content-type": "application/json" },
      },
    );
  }
}
