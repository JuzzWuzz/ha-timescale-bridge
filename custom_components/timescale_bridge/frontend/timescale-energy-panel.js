// Electricity meters report Wh. All displayed periods use the same 30-minute delta series.
const HALF_HOUR = 1_800_000;
const DAY = 48 * HALF_HOUR;
const ZONE = "Africa/Johannesburg";
const formatNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const formatDay = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: ZONE,
});
const clock = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: ZONE,
});
const colors = ["#3878bf", "#dc8c37", "#008878", "#ac619d", "#7165ac", "#9a7147"];
const displayEnergy = (wh) =>
  wh === null ? "Incomplete" : formatNumber.format(wh / 1000) + " kWh";
const isoDay = (time) => new Date(time).toISOString().slice(0, 10);
const today = () => {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(Date.now());
  const part = (type) => parts.find((p) => p.type === type).value;
  return [part("year"), part("month"), part("day")].join("-");
};

function dateRange(day, period, offset = 0) {
  // Calendar arithmetic on date-only values. The bridge zone uses UTC+02 throughout its API range.
  const date = new Date(day + "T00:00:00Z");
  if (!Number.isFinite(+date)) throw new Error("Choose a valid date.");
  if (period === "month") {
    date.setUTCDate(1);
    date.setUTCMonth(date.getUTCMonth() + offset);
  } else if (period === "week") {
    date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7) + 7 * offset);
  } else {
    date.setUTCDate(date.getUTCDate() + offset);
  }
  const first = isoDay(date);
  if (period === "month") date.setUTCMonth(date.getUTCMonth() + 1);
  else date.setUTCDate(date.getUTCDate() + (period === "week" ? 7 : 1));
  const midnight = (day) => Date.parse(day + "T00:00:00+02:00");
  return { first, start: midnight(first), end: midnight(isoDay(date)) };
}

function meterTree(response) {
  if (response?.api_version !== 1 || !Array.isArray(response.meters)) {
    throw new Error("The bridge returned an unsupported meter configuration.");
  }
  const byId = new Map();
  for (const { series_id: id, label, parent_id: parent } of response.meters) {
    if (
      !Number.isSafeInteger(id) ||
      id <= 0 ||
      byId.has(id) ||
      typeof label !== "string" ||
      !label.trim()
    ) {
      throw new Error("Check the configured meter IDs and names in Timescale Bridge.");
    }
    byId.set(id, { id, label, parent, children: [], values: [] });
  }
  const roots = [];
  for (const meter of byId.values()) {
    if (meter.parent === null) roots.push(meter);
    else if (byId.has(meter.parent)) byId.get(meter.parent).children.push(meter);
    else throw new Error("A configured meter is missing its upstream meter.");
  }
  const rows = [];
  const visit = (meter, depth) => {
    rows.push({ meter, depth });
    for (const child of meter.children) visit(child, depth + 1);
  };
  for (const root of roots) visit(root, 0);
  if (rows.length !== byId.size)
    throw new Error("The configured meters contain a circular connection.");
  return { roots, rows, meters: [...byId.values()] };
}

async function readEnergy(hass, meters, range, current) {
  // Daily chunks preserve the bridge's historical delta semantics at every display scale.
  // Request batches sequentially to respect both the 8-series and active-request limits.
  const closedEnd = Math.max(
    range.start,
    Math.min(range.end, Math.floor(Date.now() / HALF_HOUR) * HALF_HOUR),
  );
  for (let start = range.start; start < closedEnd; start += DAY) {
    const end = Math.min(start + DAY, closedEnd);
    const length = (end - start) / HALF_HOUR;
    for (let i = 0; i < meters.length; i += 8) {
      if (!current()) return;
      const batch = meters.slice(i, i + 8);
      const data = await hass.callWS({
        type: "timescale_bridge/query",
        series_ids: batch.map((m) => m.id),
        preset: "day",
        start: new Date(start).toISOString(),
        end: new Date(end).toISOString(),
      });
      if (!current()) return;
      if (
        data?.api_version !== 1 ||
        data.resolution !== "30m" ||
        Date.parse(data.start) !== start ||
        Date.parse(data.end) !== end ||
        !Array.isArray(data.series) ||
        data.series.length !== batch.length
      ) {
        throw new Error("The bridge returned an unexpected time window or resolution.");
      }
      for (const meter of batch) {
        const series = data.series.find((row) => row.id === meter.id);
        if (series?.points?.length !== length || series?.coverage?.length !== length) {
          throw new Error("The bridge returned a different set of meter readings.");
        }
        series.points.forEach(([time, value], j) => {
          if (time !== start + j * HALF_HOUR)
            throw new Error("The meter readings are out of alignment.");
          const coverage = series.coverage[j];
          meter.values.push(
            coverage?.state === "observed" &&
              coverage.live === false &&
              coverage.clipped === false &&
              Number.isFinite(value)
              ? value
              : null,
          );
        });
      }
    }
  }
  return closedEnd;
}

function total(values) {
  if (!values.length || values.some((value) => value === null)) return null;
  return values.reduce((sum, value) => sum + value, 0);
}

function residual(meter) {
  // Subtract direct children within each interval; grandchildren already belong to their parent.
  let sum = 0;
  let over = false;
  for (const [index, value] of meter.values.entries()) {
    const children = meter.children.map((child) => child.values[index]);
    if (value === null || children.some((n) => n === null)) return { value: null, over };
    const remainder = value - children.reduce((a, b) => a + b, 0);
    sum += remainder;
    if (remainder < -1e-8) over = true;
  }
  return { value: meter.values.length ? sum : null, over };
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(label, action, symbol = label) {
  const node = element("button", symbol);
  node.type = "button";
  node.title = label;
  node.setAttribute("aria-label", label);
  node.addEventListener("click", action);
  return node;
}

function select(label, values, selected, onChange) {
  const wrapper = element("label", label);
  const input = element("select");
  input.setAttribute("aria-label", label);
  for (const [value, text] of values) input.add(new Option(text, value));
  input.value = selected;
  input.addEventListener("change", () => onChange(input.value));
  wrapper.append(input);
  return wrapper;
}

class TimescaleEnergyPanel extends HTMLElement {
  constructor() {
    super();
    const shadow = this.attachShadow({ mode: "open" });
    const style = element("link");
    style.rel = "stylesheet";
    style.href = new URL("./timescale-energy.css", import.meta.url).href;
    this.header = element("header");
    this.main = element("main");
    this.main.setAttribute("aria-live", "polite");
    shadow.append(style, this.header, this.main);
    this.period = "day";
    this.bucket = 2;
    this.day = isoDay(Date.parse(today()) - DAY);
    this.generation = 0;
  }

  connectedCallback() {
    this.controls();
    this.load();
  }
  disconnectedCallback() {
    this.generation++;
  }
  set hass(value) {
    const changed =
      this.ha?.connection !== value?.connection ||
      this.ha?.connected !== value?.connected ||
      !this.ha;
    this.ha = value;
    if (this.menu) this.menu.hass = value;
    if (changed && this.isConnected) this.load();
  }
  set narrow(value) {
    this.toggleAttribute("narrow", Boolean(value));
    if (this.menu) this.menu.narrow = Boolean(value);
  }

  controls() {
    this.menu = element("ha-menu-button");
    this.menu.hass = this.ha;
    this.menu.narrow = this.hasAttribute("narrow");
    const title = element("div", undefined, "title");
    title.append(this.menu, element("h1", "Timescale Energy"));
    const controls = element("div", undefined, "controls");
    const rangeSelect = select(
      "Period",
      [
        ["day", "Day"],
        ["week", "Week"],
        ["month", "Month"],
      ],
      this.period,
      (value) => {
        this.period = value;
        this.bucket = value === "day" ? 2 : 48;
        this.controls();
        this.load();
      },
    );
    const changeDay = (direction) => {
      this.day = dateRange(this.day, this.period, direction).first;
      this.controls();
      this.load();
    };
    const label = element("label", "Date");
    const date = element("input");
    date.type = "date";
    date.min = "1970-01-02";
    date.max = today();
    date.value = this.day;
    date.addEventListener("change", () => {
      if (date.value && date.validity.valid) {
        this.day = date.value;
        this.controls();
        this.load();
      }
    });
    label.append(date);
    const next = button("Next period", () => changeDay(1), "›");
    next.disabled = dateRange(this.day, this.period, 1).first > today();
    const prev = button("Previous period", () => changeDay(-1), "‹");
    prev.disabled = dateRange(this.day, this.period, -1).first < date.min;
    controls.append(
      rangeSelect,
      prev,
      label,
      next,
      button("Refresh", () => this.load()),
    );
    this.header.replaceChildren(title, controls);
  }

  message(title, detail) {
    const card = element("section", undefined, "card message");
    card.append(element("h2", title), element("p", detail));
    this.main.replaceChildren(card);
  }

  async load() {
    const generation = ++this.generation;
    const current = () => this.isConnected && generation === this.generation;
    this.data = undefined;
    if (!this.ha?.connected) {
      this.message("Waiting for Home Assistant", "Readings will load when connected.");
      return;
    }
    this.message("Loading electricity usage…", "Reading your configured meters.");
    try {
      const range = dateRange(this.day, this.period);
      const topology = await this.ha.callWS({ type: "timescale_bridge/topology" });
      if (!current()) return;
      const tree = meterTree(topology);
      if (!tree.roots.length) {
        this.message(
          "No electricity meters configured",
          "Open Settings → Devices & services → Timescale Bridge → Configure to add a grid feed.",
        );
        return;
      }
      const closedEnd = await readEnergy(this.ha, tree.meters, range, current);
      if (!current()) return;
      this.data = { tree, range, closedEnd };
      this.renderData();
    } catch (error) {
      if (current())
        this.message(
          "Electricity usage unavailable",
          error instanceof Error
            ? error.message
            : "Check the bridge connection and meter configuration, then select Refresh.",
        );
    }
  }

  renderData() {
    const { tree, range, closedEnd } = this.data;
    const summary = element("section", undefined, "card");
    const bounds = formatDay.formatRange(range.start, range.end - 1);
    summary.append(
      element("div", bounds + " · " + ZONE, "dates"),
      element("h2", "Electricity usage"),
    );
    const amount = total(tree.roots.map((m) => total(m.values)));
    summary.append(element("strong", displayEnergy(amount), "headline"));
    const captions = ["Total across independent grid feeds."];
    if (closedEnd < range.end)
      captions.push(
        closedEnd > range.start
          ? "Closed intervals through " +
              formatDay.format(closedEnd - 1) +
              " " +
              clock.format(closedEnd) +
              "."
          : "Waiting for the first interval to close.",
      );
    if (amount === null && closedEnd > range.start)
      captions.push("Some feed readings are missing or partial.");
    summary.append(element("p", captions.join(" ")));
    const chart = element("section", undefined, "card");
    const chartHead = element("div", undefined, "card-head");
    chartHead.append(
      element("h2", "Grid usage"),
      select(
        "Group by",
        [
          [1, "Half hour"],
          [2, "Hour"],
          [48, "Day"],
        ],
        this.bucket,
        (value) => {
          this.bucket = Number(value);
          this.renderData();
        },
      ),
    );
    chart.append(chartHead, ...usageChart(tree.roots, range, this.bucket));
    const sources = element("section", undefined, "card");
    sources.append(element("h2", "Meter breakdown"));
    const feedResidual = total(tree.roots.map((m) => residual(m).value));
    sources.append(element("p", "Untracked at grid feeds: " + displayEnergy(feedResidual)));
    const table = element("table");
    const head = table.createTHead().insertRow();
    for (const name of ["Meter", "Usage", "Untracked"]) head.append(element("th", name));
    const body = table.createTBody();
    let mismatch = false;
    for (const { meter, depth } of tree.rows) {
      const row = body.insertRow();
      const name = element("th", meter.label);
      name.scope = "row";
      name.style.paddingInlineStart = 0.45 + depth * 0.85 + "rem";
      name.append(
        element(
          "small",
          depth ? "Under " + tree.meters.find((m) => m.id === meter.parent).label : "Grid feed",
        ),
      );
      const remainder = residual(meter);
      row.append(
        name,
        element("td", displayEnergy(total(meter.values))),
        element("td", meter.children.length || !depth ? displayEnergy(remainder.value) : "—"),
      );
      if (remainder.over) {
        row.className = "warning";
        mismatch = true;
      }
    }
    sources.append(
      table,
      element("p", "Untracked is the meter reading left after its direct downstream meters."),
    );
    if (mismatch)
      sources.append(
        element("p", "Downstream usage exceeds its parent in one or more intervals.", "warning"),
      );
    this.main.replaceChildren(summary, chart, sources);
  }
}

function usageChart(roots, range, size) {
  const groups = [];
  const step = size * HALF_HOUR;
  let peak = 0;
  for (let time = range.start, i = 0; time < range.end; time += step, i += size) {
    const values = roots.map((m) => {
      const slice = m.values.slice(i, i + size);
      return slice.length === size && !slice.some((n) => n < 0) ? total(slice) : null;
    });
    const sum = total(values);
    if (sum !== null) peak = Math.max(peak, sum);
    groups.push({ time, values, sum });
  }
  const width = 600,
    height = 120;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const shape = (tag, attributes, text) => {
    const node = document.createElementNS(svg.namespaceURI, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  svg.setAttribute("viewBox", "0 0 650 155");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Stacked grid usage in kilowatt-hours");
  const ceiling = peak || 1000;
  for (const fraction of [0, 0.5, 1]) {
    const y = 8 + height * (1 - fraction);
    svg.append(
      shape("line", { x1: 44, x2: 644, y1: y, y2: y, class: "grid" }),
      shape(
        "text",
        { x: 38, y: y + 4, "text-anchor": "end" },
        formatNumber.format((ceiling * fraction) / 1000),
      ),
    );
  }
  const label = (time) => (size === 48 ? formatDay.format(time) : clock.format(time));
  const barWidth = width / groups.length;
  let gaps = false;
  groups.forEach(({ time, values, sum }, i) => {
    const x = 44 + i * barWidth;
    const group = shape("g", {});
    if (sum === null) {
      const pending = time + step > Date.now();
      gaps ||= !pending;
      group.append(
        shape(
          "title",
          {},
          formatDay.format(time) +
            " " +
            clock.format(time) +
            (pending ? " · Open interval" : " · Missing or invalid readings"),
        ),
        shape(
          "text",
          { x: x + barWidth / 2, y: 126, "text-anchor": "middle" },
          pending ? "·" : "×",
        ),
      );
    } else {
      let y = 128;
      values.forEach((value, j) => {
        const h = (height * value) / ceiling;
        y -= h;
        const rect = shape("rect", {
          x,
          y,
          width: barWidth * 0.86,
          height: h,
          fill: colors[j % colors.length],
        });
        rect.append(
          shape(
            "title",
            {},
            roots[j].label +
              " · " +
              formatDay.format(time) +
              " " +
              clock.format(time) +
              " · " +
              displayEnergy(value),
          ),
        );
        group.append(rect);
      });
    }
    svg.append(group);
  });
  svg.append(
    shape("text", { x: 44, y: 149 }, label(range.start)),
    shape("text", { x: 644, y: 149, "text-anchor": "end" }, label(range.end - step)),
  );
  const legend = element("ul");
  roots.forEach((m, i) => {
    const item = element("li", m.label);
    item.style.setProperty("--feed-color", colors[i % colors.length]);
    legend.append(item);
  });
  const result = [svg, legend];
  if (gaps) result.push(element("p", "× Missing, partial or negative feed readings."));
  return result;
}

// An existing standalone resource may already provide this custom element.
if (!customElements.get("timescale-energy-panel")) {
  customElements.define("timescale-energy-panel", TimescaleEnergyPanel);
}
