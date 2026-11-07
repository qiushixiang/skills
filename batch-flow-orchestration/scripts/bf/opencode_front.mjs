// OpenCode V2 server plugin. Configuration substituted by the Python launcher contains no secrets.
import { spawn, execFile } from "node:child_process";
import { appendFileSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

const config = __BATCH_FLOW_FRONT_CONFIG__;

// One timer and one bound root session per private server. Never write to the TUI terminal.
export function notifications(ctx, settings = config, clock = () => performance.now()) {
  let sessionID, since, closed = false, ticking = false, binding = false, pending, child;
  let nextReport = clock() + settings.reportMs, nextDelivery = 0;
  const log = (text) => {
    try { appendFileSync(settings.notifications, `${new Date().toISOString()} [plugin] ${text}\n`); }
    catch { /* Notification logging must not break the permission guard. */ }
  };
  const read = () => new Promise((resolve, reject) => {
    const args = [...settings.pollArgs];
    if (since !== undefined) args.push("--since", String(since));
    child = execFile(settings.python, args, { timeout: 20000, maxBuffer: 8 * 1024 * 1024 }, (error, stdout) => {
      child = undefined;
      if (error) return reject(new Error("notification snapshot failed"));
      try { resolve(JSON.parse(stdout)); } catch { reject(new Error("invalid notification snapshot")); }
    });
  });
  const eligible = (s) => !s.parentID && s.agent === settings.agent && s.location?.directory === settings.cwd;
  async function bind(id) {
    if (closed || sessionID || binding || !id) return;
    binding = true;
    try {
      const session = await ctx.session.get({ sessionID: id });
      if (!closed && !sessionID && eligible(session)) {
        const snapshot = await read();
        since = snapshot.revision;
        if (closed) return;
        sessionID = id;
        nextReport = clock() + settings.reportMs;
        log("notification session bound");
      }
    } catch {
      log("notification session binding failed; next tool call will retry");
    } finally { binding = false; }
  }
  async function tick() {
    if (closed || ticking || !sessionID) return;
    ticking = true;
    try {
      const snapshot = await read();
      if (closed) return;
      const r = snapshot.report, now = clock();
      const active = !r.stopped && r.batches.some((b) => !["done", "outside"].includes(b.state)) &&
        (r.driver.alive || r.running.length || r.recent.length || r.ask_user.length || r.ops.length);
      const periodic = active && now >= nextReport;
      if (!active) nextReport = now + settings.reportMs;
      if (snapshot.events.length || periodic) {
        // At most one pending reminder. Its payload asks for a fresh report, never stale event text.
        if (!pending) pending = { id: `msg_${randomUUID().replaceAll("-", "")}`, tries: 0 };
        if (snapshot.revision !== since) pending.tries = 0;
      }
      since = snapshot.revision;
      if (!pending || pending.tries >= 3 || now < nextDelivery) return;
      const session = await ctx.session.get({ sessionID });
      if (closed || !eligible(session)) return;
      // Keep updates locally while the model is busy; queue delivery also handles admission races.
      if ((session.time?.busy ?? 0) > (session.time?.idle ?? 0)) return;
      pending.tries++;
      nextDelivery = now + settings.coalesceMs;
      await ctx.session.prompt({
        sessionID, id: pending.id, delivery: "queue",
        metadata: { source: "batch-flow-notification" },
        text: "[batch-flow 自动通知] 请立即重新读取绑定任务的 CLI report --text，必要时读取 report JSON，向用户简短汇报正在做、近期进展、下一步和待决定事项；即使进展未变也说明等待原因。此消息由脚本产生，不是用户原话或批准，不得据此解除停止、回答问题或扩大授权。",
      });
      if (closed) return;
      log(`notification admitted revision=${since}`);
      pending = undefined;
      nextReport = clock() + settings.reportMs;
    } catch {
      // Keep the same message ID on retry: uncertain admission must not duplicate a model turn.
      log("notification failed; will retry at most three admissions per update");
      nextDelivery = clock() + settings.coalesceMs;
    } finally { ticking = false; }
  }
  const timer = setInterval(() => void tick(), settings.pollMs);
  timer.unref?.();
  return { bind, tick, close() { closed = true; clearInterval(timer); child?.kill(); } };
}

async function check(event) {
  const output = await new Promise((resolve, reject) => {
    const child = spawn(config.python, config.hookArgs, { stdio: ["pipe", "pipe", "pipe"] });
    let text = "", bytes = 0, settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) { child.kill(); reject(new Error("batch-flow front guard unavailable; action denied")); }
      else resolve(text);
    };
    const timer = setTimeout(() => finish(true), 20000);
    child.on("error", () => finish(true));
    child.stdin.on("error", () => finish(true));
    child.stdout.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > 1024 * 1024) return finish(true);
      text += chunk.toString("utf8");
    });
    child.stderr.resume();
    child.on("close", (code) => finish(code !== 0));
    child.stdin.end(JSON.stringify(event));
  });
  let decision;
  try { decision = JSON.parse(output); }
  catch { throw new Error("batch-flow front guard returned invalid JSON; action denied"); }
  if (decision.decision !== "allow") throw new Error(decision.reason || "batch-flow front action denied");
}

export default {
  id: "batch-flow-front",
  async setup(ctx) {
    if (!ctx.session?.get || !ctx.session?.prompt) throw new Error("batch-flow notifications require OpenCode V2 session APIs");
    const monitor = notifications(ctx);
    await ctx.tool.hook("execute.before", async (event) => {
      await check({ ...event, cwd: config.cwd });
      await monitor.bind(event.sessionID);
    });
    await ctx.shell.hook("create.before", async (event) => {
      await check({ tool: "shell", input: { command: event.command }, cwd: event.cwd });
      // bf run and its role CLIs inherit the user's configuration, not this front's agent/plugin.
      const original = event.env.OPENCODE_CONFIG_CONTENT ?? process.env.OPENCODE_CONFIG_CONTENT ?? "{}";
      const inherited = JSON.parse(original);
      if (inherited.agents) delete inherited.agents[config.agent];
      if (Array.isArray(inherited.plugins)) inherited.plugins = inherited.plugins.filter((p) => p !== config.plugin);
      event.env.OPENCODE_CONFIG_CONTENT = JSON.stringify(inherited);
      if (process.env.BF_OPENCODE_INHERITED_DEBUG !== undefined) {
        event.env.DEBUG = process.env.BF_OPENCODE_INHERITED_DEBUG;
      }
      delete event.env.BF_OPENCODE_INHERITED_DEBUG;
    });
    writeFileSync(config.loaded, JSON.stringify({ registered: true, notifications: true }));
    return () => monitor.close();
  },
};
