"use strict";

function filterDeviceSlugs(slugs, query) {
  const needle = query.trim().toLowerCase();
  return slugs.filter(slug => slug.toLowerCase().includes(needle));
}
