"use strict";

function stackMarkerSize(count, locations = 1) {
  return Math.max(locations > 1 ? 46 : 38, Math.min(62, 34 + Math.log2(count) * 3));
}

function groupScreenMarkers(items, gap = 6) {
  let groups = items.map(item => ({
    x: item.x,
    y: item.y,
    radius: item.radius,
    count: item.count,
    members: [item]
  })).sort((a, b) => a.x - b.x || a.y - b.y);

  while (groups.length > 1) {
    const cellSize = 2 * Math.max(...groups.map(group => group.radius)) + gap;
    const cells = new Map();
    const parents = groups.map((_, index) => index);
    function root(index) {
      while (parents[index] !== index) {
        parents[index] = parents[parents[index]];
        index = parents[index];
      }
      return index;
    }
    let merged = false;
    groups.forEach((group, index) => {
      const cellX = Math.floor(group.x / cellSize);
      const cellY = Math.floor(group.y / cellSize);
      for (let dx = -1; dx <= 1; dx += 1) {
        for (let dy = -1; dy <= 1; dy += 1) {
          for (const otherIndex of cells.get(`${cellX + dx},${cellY + dy}`) || []) {
            const a = root(index);
            const b = root(otherIndex);
            if (a === b) continue;
            const other = groups[otherIndex];
            const distance = Math.hypot(group.x - other.x, group.y - other.y);
            if (distance <= group.radius + other.radius + gap) {
              parents[a] = b;
              merged = true;
            }
          }
        }
      }
      const key = `${cellX},${cellY}`;
      if (!cells.has(key)) cells.set(key, []);
      cells.get(key).push(index);
    });
    if (!merged) return groups;

    const components = new Map();
    groups.forEach((group, index) => {
      const key = root(index);
      if (!components.has(key)) components.set(key, []);
      components.get(key).push(...group.members);
    });
    groups = Array.from(components.values(), members => {
      if (members.length === 1) {
        const item = members[0];
        return { x: item.x, y: item.y, radius: item.radius, count: item.count, members };
      }
      const count = members.reduce((sum, item) => sum + item.count, 0);
      return {
        x: members.reduce((sum, item) => sum + item.x, 0) / members.length,
        y: members.reduce((sum, item) => sum + item.y, 0) / members.length,
        radius: stackMarkerSize(count, members.length) / 2,
        count,
        members
      };
    });
    // A merged badge can overlap a neighbour after its centre or size changes.
  }
  return groups;
}
