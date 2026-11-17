'use strict';
// Gedeeld door index.html en recensie.html: de link naar Trakt,
// cijferkleuren en kleine opmaakhulpjes.

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

const dec = n => String(n).replace('.', ',');

function cleanTitle(title) {
  return title.replace(/\s*\(\d{4}\)[^(]*$/, '').trim();
}

// Trakt-pagina als de scraper die vond, anders zoeken op titel (zonder jaartal)
function traktUrl(r) {
  if (r.traktSlug) return `https://app.trakt.tv/movies/${encodeURIComponent(r.traktSlug)}`;
  return `https://app.trakt.tv/search?q=${encodeURIComponent(cleanTitle(r.title))}`;
}

function badgeClass(r) {
  if (r === null) return 'b-none';
  if (r >= 4.5)  return 'b-top';
  if (r >= 3.5)  return 'b-high';
  if (r >= 2.5)  return 'b-mid';
  return 'b-low';
}
