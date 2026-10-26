// Login panel: a lattice of ◆ tiles flooded by diamond-shaped colour waves from a central gate.
// Run: node animation/login.ts  (writes public/login.json; no fonts or assets, so no .clayzo needed)
import { writeFileSync } from "node:fs";
import {
  ANIMATION_DOCUMENT_VERSION, assertValidAnimationDocument, base, constant, fill, hex, keyed,
  marker, parallaxLayersMacro, withComputedRevision, withInteraction, type Key, type SceneNode,
} from "@clayzo/animation/authoring";

const W = 1000, H = 1250, CX = W / 2, CY = H / 2;
const FIELD = hex("#101010");
const WAVES = ["#F0FE48", "#691CB9", "#FF8827", "#1BD660"].map(hex);

const TPF = 4; // 240 ticks/s at 60 fps
const snap = (t: number) => Math.round(t / TPF) * TPF;
const WAVE = 480; // 2 s between waves
const DURATION = WAVE * WAVES.length; // 8 s loop
const DIP = 36, SWELL = 84; // tile dips into the cut, then swells back
const START = 48; // hero cut of wave 0

const IN = { x1: 0.5, y1: 0, x2: 0.7, y2: 0.7 };
const OUT = { x1: 0.2, y1: 0.45, x2: 0.45, y2: 1 };
const SNAP = { x1: 0.45, y1: 0, x2: 0.55, y2: 1 };
const s3 = (v: number) => ({ x: v, y: v, z: 1 });

// One track pair per diamond: for each wave, dip, cut colour at the bottom, swell back.
// The colour before the first cut is the last wave's, so tick 0 matches the loop end.
function pulses(id: string, first: number, depth: number, peak?: number) {
  const scale: Key<{ x: number; y: number; z: number }>[] = [];
  const color: Key<ReturnType<typeof hex>>[] = [[0, WAVES[WAVES.length - 1], "hold"]];
  WAVES.forEach((c, k) => {
    const cut = snap(first + k * WAVE);
    scale.push([cut - DIP, s3(1), IN], [cut, s3(depth), OUT]);
    if (peak) scale.push([cut + SWELL / 2, s3(peak), SNAP]);
    scale.push([cut + SWELL, s3(1)]);
    color.push([cut, c, "hold"]);
  });
  if (scale[0][0] > 0) scale.unshift([0, s3(1)]);
  return { scale: keyed(`${id}.scale`, scale), color: keyed(`${id}.color`, color) };
}

const nodes: Record<string, SceneNode> = {
  field: { ...base("field", { outTick: DURATION, x: CX, y: CY }), type: "rect",
           size: constant({ width: W + 400, height: H + 400 }), cornerRadius: constant(0), style: { fill: fill(FIELD) } },
};

// Diamond lattice: a square grid turned 45°, rows at half pitch with alternate offsets.
const PITCH = 160, TILE = 88, HERO = 283, CLEAR = HERO / Math.SQRT2 + 90, MARGIN = 160;
// px of L1 distance per tick, so even the farthest tile settles before the next wave (and the loop end).
const SPEED = (CX + CY + 2 * MARGIN - CLEAR) / (WAVE - SWELL - START - 24 - TPF);
const tiles: string[] = [];
for (let j = -Math.ceil((CY + MARGIN) / (PITCH / 2)); j * (PITCH / 2) <= CY + MARGIN; j++) {
  for (let i = -Math.ceil((CX + MARGIN) / PITCH) - 1; i * PITCH <= CX + MARGIN; i++) {
    const dx = i * PITCH + (j & 1 ? PITCH / 2 : 0), dy = j * (PITCH / 2);
    const l1 = Math.abs(dx) + Math.abs(dy);
    if (l1 < CLEAR || Math.abs(dx) > CX + MARGIN) continue;
    const id = `t${tiles.length}`;
    const p = pulses(id, START + 24 + (l1 - CLEAR) / SPEED, 0.55);
    nodes[id] = { ...base(id, { outTick: DURATION, x: dx, y: dy, rotation: 45, scale: p.scale }), type: "rect",
                  size: constant({ width: TILE, height: TILE }), cornerRadius: constant(10), style: { fill: fill(p.color) } };
    tiles.push(id);
  }
}
nodes.lattice = { ...base("lattice", { outTick: DURATION, x: CX, y: CY }), type: "group", children: tiles };

// The gate: the hero pops with the piece's one overshoot as it releases each wave.
const hp = pulses("gate", START, 0.86, 1.06);
nodes.gate = { ...base("gate", { outTick: DURATION, x: CX, y: CY, rotation: 45, scale: hp.scale }), type: "rect",
               size: constant({ width: HERO, height: HERO }), cornerRadius: constant(28), style: { fill: fill(hp.color) } };

const scene = withComputedRevision({
  version: ANIMATION_DOCUMENT_VERSION, id: "login-gate", name: "Login gate",
  canvas: { width: W, height: H, pixelAspectRatio: 1, backgroundColor: FIELD },
  timing: { ticksPerSecond: 240, frameRate: { numerator: 60, denominator: 1 }, durationTicks: DURATION, displayStartTick: 0 },
  assets: {}, rootCompositionId: "main", components: {},
  compositions: { main: { id: "main", name: "Main", width: W, height: H, durationTicks: DURATION, nodes,
                          rootNodeIds: ["field", "lattice", "gate"] } },
  markers: WAVES.map((_, k) => marker(`wave${k}`, snap(START + k * WAVE))),
  design: { tokens: { looping: true } },
  capabilities: { required: [], optional: [], unsupportedPolicy: "warn" },
});

const document = withComputedRevision(withInteraction(scene, [
  parallaxLayersMacro({ nodeIds: ["lattice", "gate"], span: 500, reach: 28 }),
]));
assertValidAnimationDocument(document);
writeFileSync(new URL("../public/login.json", import.meta.url), JSON.stringify(document));
console.log(`tiles=${tiles.length} duration=${DURATION}`);
