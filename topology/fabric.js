const byId = (id) => document.getElementById(id);
const tabs = [...document.querySelectorAll("[data-phase]")];
const play = byId("play");
const step = byId("step");
const packet = byId("packet");
const titles = {
  baseline: ["Both uplinks available", "Both leaves have two paths to the remote host subnet."],
  fault: ["One uplink disabled", "Leaf 2 keeps both neighbours. Its return route uses the surviving spine."],
  restored: ["Both routes restored", "Both leaves have recovered their second next hop."],
  live: ["Live forwarding state", "The map follows the installed routes and Full adjacencies."],
};
const uplinks = ["to_s1", "to_s2"];
let recording = [];
let state = null;
let selected = "baseline";
let currentUplink = null;
let sequences = [];
let position = 0;
let busy = false;
let request = 0;
let refreshPending = false;

function showPacket(sequence, x, y) {
  packet.setAttribute("visibility", "visible");
  packet.setAttribute("transform", `translate(${x} ${y})`);
  byId("packet-label").textContent = sequence;
}

function pathPoints(uplink) {
  return [[238, 380], [238, 224], [uplink === "to_s1" ? 238 : 522, 79], [522, 224], [522, 380]];
}

function advance(sequence, uplink) {
  const points = pathPoints(uplink);
  const lengths = points.slice(1).map(([x, y], i) => Math.hypot(x - points[i][0], y - points[i][1]));
  const total = lengths.reduce((a, b) => a + b, 0);
  const duration = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 680;
  const start = performance.now();

  return new Promise((resolve) => {
    function draw(now) {
      const progress = Math.min(1, (now - start) / duration);
      let distance = Math.max(0, progress * total);
      let edge = 0;
      while (edge < lengths.length - 1 && distance > lengths[edge]) {
        distance -= lengths[edge];
        edge++;
      }
      const [x, y] = points[edge];
      const [nextX, nextY] = points[edge + 1];
      const fraction = distance / lengths[edge];
      showPacket(sequence, x + (nextX - x) * fraction, y + (nextY - y) * fraction);
      if (progress < 1) requestAnimationFrame(draw);
      else resolve();
    }
    requestAnimationFrame(draw);
  });
}

function updateMap() {
  const down = state.disabled ? [state.disabled_uplink] : state.links.leaf1.down || [];
  for (const [number, leaf] of [[1, state.links.leaf1], [2, state.links.leaf2]]) {
    for (const uplink of uplinks) {
      const line = byId(`l${number}-${uplink}`);
      const enabled = leaf.neighbours.some((entry) => entry[2] === uplink);
      const disabled = number === 1 ? down.includes(uplink) : (leaf.down || []).includes(uplink);
      line.setAttribute("class", disabled ? "down" : enabled ? (uplink === currentUplink ? "active" : "") : "inactive");
    }
  }
  const marker = byId("fault-marker");
  marker.setAttribute("visibility", down.length ? "visible" : "hidden");
  marker.setAttribute("transform", down[0] === "to_s2" ? "translate(381 151)" : "translate(238 149)");
  for (const id of ["access1", "access2"]) byId(id).classList.toggle("active", !!currentUplink);
}

function updateRoutes() {
  const panel = byId("routes");
  panel.replaceChildren();
  for (const [number, leaf] of [[1, state.links.leaf1], [2, state.links.leaf2]]) {
    const article = document.createElement("div");
    article.className = "route";
    const prefix = leaf.route.split(/\s/)[0];
    const hops = [...leaf.route.matchAll(/\bvia (\S+) dev (\S+)/g)];
    const title = document.createElement("h3");
    title.textContent = `Leaf ${number} · ${leaf.neighbours.length} Full`;
    const caption = document.createElement("p");
    caption.textContent = prefix ? `${prefix} · ${hops.length} next hop${hops.length === 1 ? "" : "s"}` : "No remote route installed";
    const list = document.createElement("ul");
    for (const [, address, device] of hops) {
      const row = document.createElement("li");
      row.textContent = `${device} via ${address}`;
      list.append(row);
    }
    article.append(title, caption, list);
    panel.append(article);
  }
}

function updateControls() {
  const live = selected === "live";
  for (const tab of tabs) {
    tab.disabled = busy;
    tab.setAttribute("aria-pressed", String(tab.dataset.phase === selected));
  }
  play.disabled = busy || !state || (!live && !sequences.length);
  step.disabled = busy || !state || live || !sequences.length;
  play.textContent = live ? "Send 6 pings" : "Play capture";
  step.hidden = live;
  byId("packet-count").textContent = live ? `${position} captured` : `${position} / ${sequences.length} packets`;
}

function render() {
  const [title, description] = titles[selected];
  byId("stage").textContent = title;
  byId("explanation").textContent = description;
  updateRoutes();
  updateMap();
  updateControls();
}

async function loadLive(version) {
  if (refreshPending) return;
  refreshPending = true;
  try {
    const response = await fetch("/api/live");
    const data = await response.json();
    if (version !== request || busy) return;
    if (!response.ok) throw new Error(data.error);
    state = data;
    if (currentUplink && !state.links.leaf1.neighbours.some((entry) => entry[2] === currentUplink)) {
      currentUplink = null;
      packet.setAttribute("visibility", "hidden");
    }
    render();
  } catch (error) {
    if (version !== request) return;
    byId("explanation").textContent = error.message;
    state = null;
    updateControls();
  } finally {
    refreshPending = false;
  }
}

function select(phase) {
  const version = ++request;
  selected = phase;
  state = recording.find((item) => item.phase === phase) || null;
  currentUplink = state?.leaf1_forward_uplink || null;
  position = 0;
  packet.setAttribute("visibility", "hidden");
  sequences = state?.packets.map((line) => Number(line.split("\t").at(-1))) || [];
  byId("source").textContent = state ? "Recorded capture" : "Live lab";
  byId("capture").textContent = state
    ? `Leaf 1 ${currentUplink} · ${sequences.length} echo requests`
    : "Send a test flow from Host 1 to Host 2.";
  byId("summary").textContent = state?.ping.split("\n").find((line) => line.includes("packets transmitted")) || "";
  if (state) {
    render();
  } else {
    byId("stage").textContent = titles.live[0];
    byId("explanation").textContent = "Reading the routing tables…";
    byId("routes").replaceChildren();
    document.querySelectorAll(".links path").forEach((line) => line.removeAttribute("class"));
    byId("fault-marker").setAttribute("visibility", "hidden");
    updateControls();
    loadLive(version);
  }
}

step.addEventListener("click", () => {
  position = position % sequences.length + 1;
  const [, leaf, spine] = pathPoints(currentUplink);
  showPacket(sequences[position - 1], (leaf[0] + spine[0]) / 2, (leaf[1] + spine[1]) / 2);
  updateControls();
});

async function liveProbe() {
  currentUplink = null;
  position = 0;
  updateMap();
  updateControls();
  byId("summary").textContent = "";
  byId("capture").textContent = "Starting the capture…";
  const response = await fetch("/api/probe", { method: "POST" });
  if (!response.ok || !response.body) throw new Error("Could not start a capture.");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let pending = "";
  let animation = Promise.resolve();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    pending += value;
    const lines = pending.split("\n");
    pending = lines.pop();
    for (const line of lines) {
      const event = JSON.parse(line);
      if (event.uplink) {
        currentUplink = event.uplink;
        updateMap();
        byId("capture").textContent = `Capturing on Leaf 1 ${currentUplink}`;
      }
      if (event.sequence) {
        position++;
        updateControls();
        animation = animation.then(() => advance(event.sequence, currentUplink));
      }
      if (event.summary) {
        byId("capture").textContent = `Leaf 1 ${currentUplink} · ${event.captured} echo requests`;
        byId("summary").textContent = event.summary;
      }
      if (event.error) byId("summary").textContent = event.error;
    }
  }
  await animation;
}

play.addEventListener("click", async () => {
  busy = true;
  updateControls();
  try {
    if (selected === "live") {
      packet.setAttribute("visibility", "hidden");
      await liveProbe();
    } else {
      position = 0;
      for (const sequence of sequences) {
        position++;
        updateControls();
        await advance(sequence, currentUplink);
      }
    }
  } catch (error) {
    byId("summary").textContent = error.message;
  } finally {
    busy = false;
    updateControls();
  }
});

for (const tab of tabs) tab.addEventListener("click", () => select(tab.dataset.phase));
setInterval(() => {
  if (selected === "live" && !busy) loadLive(request);
}, 2200);

fetch("/recording.json")
  .then((response) => {
    if (!response.ok) throw new Error("The walkthrough recording is unavailable.");
    return response.json();
  })
  .then((data) => {
    recording = data;
    select("baseline");
  })
  .catch((error) => {
    updateControls();
    byId("explanation").textContent = error.message;
  });
