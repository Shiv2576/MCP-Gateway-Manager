// Login panel: a keyhole cut through a cobalt field. Behind it, rows of tool blocks drift at three
// depths and shift with the cursor; on sign-in the keyhole swallows the panel.
// States: intro (pops open) -> idle (12 s seamless drift) -> open (fired by the page as `unlock`).
// Run: node animation/login.ts  (writes public/login.json; no fonts or assets, so no .clayzo needed)
import { writeFileSync } from "node:fs";
import {
  ANIMATION_DOCUMENT_VERSION, assertValidAnimationDocument, base, constant, fill, hex, keyed, marker,
  parallaxLayersMacro, pen, stateMachine, withComputedRevision, withInteraction, withStateMachine,
  type PathContour, type SceneNode,
} from "@clayzo/animation/authoring";

const W = 1000, H = 1250;
const FIELD = hex("#3A3AFF"), VOID = hex("#101010"), WHITE = hex("#FDFDFD"), ACID = hex("#F0FE48");

// Timeline at 240 ticks/s, 60 fps (4 ticks a frame).
const INTRO = 160, LOOP = 2880, OPEN = 160;
const IDLE_AT = INTRO, OPEN_AT = INTRO + LOOP, END = OPEN_AT + OPEN;

const OUT = { x1: 0.2, y1: 0.45, x2: 0.45, y2: 1 };
const SNAP = { x1: 0.45, y1: 0, x2: 0.55, y2: 1 };
const SETTLE = { x1: 0.25, y1: 0.5, x2: 0.45, y2: 1 };
const DIVE = { x1: 0.6, y1: 0, x2: 0.3, y2: 1 };
const s3 = (v: number) => ({ x: v, y: v, z: 1 });
const p3 = (x: number, y = 0) => ({ x, y, z: 0 });

// Keyhole, local space centred on the bow's centre: a circle flowing into a flared stem.
const R = 170, NECK = 70, FOOT = 140, DEPTH = 480;
const join = (Math.atan2(Math.sqrt(R * R - NECK * NECK), NECK) * 180) / Math.PI; // where the stem meets the bow
const keyhole: PathContour = pen(-FOOT, DEPTH).lineTo(-NECK, 0).arc({ x: 0, y: 0 }, R, 180 - join, 360 + join)
  .lineTo(FOOT, DEPTH).close();
const KX = W / 2, KY = 420;

const pill = (x: number, w: number, r: number): PathContour =>
  pen(x + r, -r).lineTo(x + w - r, -r).arc({ x: x + w - r, y: 0 }, r, -90, 90).lineTo(x + r, r)
    .arc({ x: x + r, y: 0 }, r, 90, 270).close();

const nodes: Record<string, SceneNode> = {
  field: { ...base("field", { outTick: END, x: W / 2, y: H / 2 }), type: "rect",
           size: constant({ width: W + 200, height: H + 200 }), cornerRadius: constant(0), style: { fill: fill(FIELD) } },
  // The clip: pops open with the piece's one overshoot, then dives past the frame on unlock.
  keyhole: { ...base("keyhole", { outTick: END, x: KX, y: KY, scale: keyed("keyhole.scale", [
               [8, s3(0.001), OUT], [76, s3(1.06), "linear"], [104, s3(1.07), SNAP], [128, s3(1)],
               [OPEN_AT, s3(1), SNAP], [OPEN_AT + 24, s3(0.94), DIVE], [OPEN_AT + 144, s3(7.5)]]) }),
             type: "path", path: constant({ contours: [keyhole] }), style: { fill: fill(VOID) } },
  void: { ...base("void", { outTick: END, x: W / 2, y: H / 2 }), type: "rect",
          size: constant({ width: W + 200, height: H + 200 }), cornerRadius: constant(0), style: { fill: fill(VOID) } },
};

// Depth layers, back to front: thin cobalt, mid white, big acid. Each row is one path of pills
// that drifts a whole number of pattern periods per loop, so the loop closes exactly.
const LAYERS = [
  { id: "back", color: FIELD, h: 28, pitch: 52, gap: 22, laps: 1, widths: [60, 140, 90, 40, 200, 110, 70, 160] },
  { id: "mid", color: WHITE, h: 48, pitch: 124, gap: 30, laps: 2, widths: [120, 60, 220, 90, 160] },
  { id: "front", color: ACID, h: 72, pitch: 300, gap: 70, laps: 3, widths: [180, 90, 260, 120] },
];
const REACH_X = 360, TOP = -280, BOTTOM = 540; // what the keyhole, parallax and the dive can show
const layerIds: string[] = [];
for (const L of LAYERS) {
  const rows: string[] = [];
  const period = L.widths.reduce((a, w) => a + w + L.gap, 0);
  for (let r = 0, y = TOP + (L.id === "mid" ? 30 : 0); y <= BOTTOM; r++, y += L.pitch) {
    const contours: PathContour[] = [];
    for (let x = -REACH_X - period, k = r * 3; x < REACH_X + period; k++) {
      const w = L.widths[k % L.widths.length];
      contours.push(pill(x, w, L.h / 2));
      x += w + L.gap;
    }
    const id = `${L.id}${r}`, dir = r % 2 ? 1 : -1;
    nodes[id] = { ...base(id, { outTick: END, position: keyed(`${id}.x`, [
                    [IDLE_AT, p3(0, y), "linear"], [OPEN_AT, p3(dir * L.laps * period, y)]]) }),
                  type: "path", path: constant({ contours }), style: { fill: fill(L.color) } };
    rows.push(id);
  }
  nodes[L.id] = { ...base(L.id, { outTick: END }), type: "group", children: rows };
  layerIds.push(L.id);
}

// The world behind the door settles from a little deep as it opens, and rushes forward on unlock.
nodes.inside = { ...base("inside", { outTick: END, x: KX, y: KY, scale: keyed("inside.scale", [
                   [8, s3(1.25), SETTLE], [144, s3(1)], [OPEN_AT + 24, s3(1), DIVE], [OPEN_AT + 144, s3(1.9)]]) }),
                 type: "group", children: layerIds };
nodes.view = { ...base("view", { outTick: END }), type: "group", clipPathId: "keyhole", children: ["void", "inside"] };

const scene = withComputedRevision({
  version: ANIMATION_DOCUMENT_VERSION, id: "login-keyhole", name: "Login keyhole",
  canvas: { width: W, height: H, pixelAspectRatio: 1, backgroundColor: FIELD },
  timing: { ticksPerSecond: 240, frameRate: { numerator: 60, denominator: 1 }, durationTicks: END, displayStartTick: 0 },
  assets: {}, rootCompositionId: "main", components: {},
  compositions: { main: { id: "main", name: "Main", width: W, height: H, durationTicks: END, nodes,
                          rootNodeIds: ["field", "view"] } },
  markers: [
    marker("intro", 0, { durationTicks: INTRO }),
    marker("idle", IDLE_AT, { durationTicks: LOOP, payload: { loop: true } }),
    marker("open", OPEN_AT, { durationTicks: OPEN, cut: true }),
  ],
  design: { tokens: { looping: false } },
  capabilities: { required: [], optional: [], unsupportedPolicy: "warn" },
});

const machine = stateMachine({ initial: "intro" })
  .state("intro", { marker: "intro", playback: "once" })
  .state("idle", { marker: "idle", playback: "loop" })
  .state("open", { marker: "open", playback: "once" })
  .on("complete", "intro", "idle")
  .on("input:unlock", "intro", "open")
  .on("input:unlock", "idle", "open");

const document = withStateMachine(
  withInteraction(scene, [parallaxLayersMacro({ nodeIds: layerIds, span: 500, reach: 36 })]),
  machine, { inputs: { unlock: "trigger" } },
);
assertValidAnimationDocument(document);
writeFileSync(new URL("../public/login.json", import.meta.url), JSON.stringify(document));
console.log(`nodes=${Object.keys(nodes).length} end=${END}`);
