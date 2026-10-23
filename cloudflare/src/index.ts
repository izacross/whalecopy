// Keeps one whalecopy container running on Cloudflare Containers.
//
// The bot polls on its own and serves no HTTP, so this Worker only:
//   - starts the container from a cron (every minute) if it isn't running,
//   - keeps it from sleeping,
//   - exposes /status, /start, /stop and /restart behind ADMIN_TOKEN.
import { Container } from "@cloudflare/containers";

// `Env` (vars and bindings) is generated into worker-configuration.d.ts by `wrangler types`.
// Secrets aren't in that file, so they're read through this wider view.
type Secrets = Record<string, unknown> & { ADMIN_TOKEN?: string };
const withSecrets = (env: Env) => env as unknown as Secrets;

// Bindings meant for this Worker, not for the bot.
const WORKER_ONLY = new Set(["WHALECOPY", "ADMIN_TOKEN"]);

// Every string var and secret becomes an env var for the bot (MODE, WALLETS, R2_*, ...),
// so any setting can be changed with `wrangler secret put NAME` and a restart.
function botEnv(env: Env): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(withSecrets(env))) {
    if (typeof value === "string" && !WORKER_ONLY.has(key)) out[key] = value;
  }
  return out;
}

export class WhaleCopy extends Container<Env> {
  // Never sleep: the bot gets no requests, so the activity timer would otherwise stop it.
  sleepAfter = "1h";

  override async onActivityExpired(): Promise<void> {
    this.renewActivityTimeout();
  }

  override onStop({ exitCode, reason }: { exitCode: number; reason: string }): void {
    console.log(`whalecopy container stopped (exit ${exitCode}, ${reason}); cron restarts it`);
  }

  async ensureRunning(): Promise<string> {
    if (await this.ctx.storage.get<boolean>("stopped")) return "stopped (by /stop)";
    if (!this.ctx.container?.running) {
      await this.start({ envVars: botEnv(this.env) });
      return "started";
    }
    return "running";
  }

  async halt(): Promise<void> {
    await this.ctx.storage.put("stopped", true);
    if (this.ctx.container?.running) await this.stop(); // SIGTERM; Litestream syncs then exits
  }

  async unhalt(): Promise<string> {
    await this.ctx.storage.delete("stopped");
    return this.ensureRunning();
  }

  // Stops the container; the next cron run starts it again with current vars and secrets.
  async restart(): Promise<void> {
    if (this.ctx.container?.running) await this.stop();
  }

  async status(): Promise<Record<string, unknown>> {
    return {
      ...(await this.getState()),
      running: this.ctx.container?.running ?? false,
      stoppedByAdmin: Boolean(await this.ctx.storage.get<boolean>("stopped")),
      mode: this.env.MODE,
    };
  }
}

function bot(env: Env) {
  return env.WHALECOPY.getByName("whalecopy"); // one fixed name: always the same single bot
}

function authorized(request: Request, env: Env): boolean {
  const token = withSecrets(env).ADMIN_TOKEN;
  if (!token) return false;
  const enc = new TextEncoder();
  const got = enc.encode(request.headers.get("Authorization") ?? "");
  const want = enc.encode(`Bearer ${token}`);
  return got.byteLength === want.byteLength && crypto.subtle.timingSafeEqual(got, want);
}

export default {
  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    console.log(`cron: ${await bot(env).ensureRunning()}`);
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    if (!authorized(request, env)) return new Response("not found", { status: 404 });
    const { pathname } = new URL(request.url);
    const stub = bot(env);

    if (request.method === "GET" && pathname === "/status") {
      return Response.json(await stub.status());
    }
    if (request.method === "POST") {
      switch (pathname) {
        case "/start":
          return Response.json({ result: await stub.unhalt() });
        case "/stop":
          await stub.halt();
          return Response.json({ result: "stopping; stays stopped until POST /start" });
        case "/restart":
          await stub.restart();
          return Response.json({ result: "stopping; cron starts it again within a minute" });
      }
    }
    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
