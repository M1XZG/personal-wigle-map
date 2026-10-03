"use strict";

function screenMarkersAtPoint(items, point, tolerance = 6) {
  // Match each dot to the click, never to another matched dot.
  return items
    .map(item => ({ item, distance: Math.hypot(item.x - point.x, item.y - point.y) }))
    .filter(match => match.distance <= match.item.radius + tolerance)
    .sort((a, b) => a.distance - b.distance
      || a.item.x - b.item.x || a.item.y - b.item.y)
    .map(match => match.item);
}
