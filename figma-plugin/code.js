// Agency Deliverables — Figma Plugin
// Generates Mood Boards, Sitemap, and Brand Guidelines pages
// from AI workflow JSON exports.

figma.showUI(__html__, { width: 420, height: 400, title: 'Agency Deliverables' });

// ── Shared colors / typography ────────────────────────────────────────────────
var WHITE      = { r: 1,     g: 1,     b: 1     };
var DARK       = { r: 0.102, g: 0.110, b: 0.141 };
var GRAY       = { r: 0.45,  g: 0.45,  b: 0.50  };
var LIGHT_GRAY = { r: 0.88,  g: 0.88,  b: 0.90  };
var BG         = { r: 0.953, g: 0.957, b: 0.965 };

function hexToRgb(hex) {
  var r = parseInt(hex.slice(1,3), 16) / 255;
  var g = parseInt(hex.slice(3,5), 16) / 255;
  var b = parseInt(hex.slice(5,7), 16) / 255;
  return { r: r, g: g, b: b };
}

function darken(rgb, amount) {
  return { r: Math.max(0, rgb.r - amount), g: Math.max(0, rgb.g - amount), b: Math.max(0, rgb.b - amount) };
}

// ── Font loading ──────────────────────────────────────────────────────────────
async function loadFont(family, style) {
  try {
    await figma.loadFontAsync({ family: family, style: style });
    return { family: family, style: style };
  } catch(e) {
    await figma.loadFontAsync({ family: 'Inter', style: style });
    return { family: 'Inter', style: style };
  }
}

async function loadCoreFonts() {
  await figma.loadFontAsync({ family: 'Inter', style: 'Regular' });
  await figma.loadFontAsync({ family: 'Inter', style: 'Medium' });
  await figma.loadFontAsync({ family: 'Inter', style: 'Semi Bold' });
  await figma.loadFontAsync({ family: 'Inter', style: 'Bold' });
}

// ── Page management ───────────────────────────────────────────────────────────
async function getOrReplacePage(pageName) {
  var page = null;
  for (var i = 0; i < figma.root.children.length; i++) {
    if (figma.root.children[i].name === pageName) {
      page = figma.root.children[i];
      break;
    }
  }
  if (page) {
    figma.currentPage = page;
    var kids = page.children.slice();
    for (var j = 0; j < kids.length; j++) kids[j].remove();
  } else {
    page = figma.createPage();
    page.name = pageName;
    figma.currentPage = page;
  }
  return page;
}

// ── Helper: create text node ──────────────────────────────────────────────────
function makeText(chars, font, size, color, opts) {
  var t = figma.createText();
  t.fontName = font;
  t.fontSize = size;
  // Truncate if needed
  if (opts && opts.maxChars && chars.length > opts.maxChars) {
    chars = chars.slice(0, opts.maxChars - 1) + '…';
  }
  t.characters = chars || ' ';
  t.fills = [{ type: 'SOLID', color: color || DARK }];
  if (opts && opts.opacity !== undefined) t.opacity = opts.opacity;
  if (opts && opts.x !== undefined) t.x = opts.x;
  if (opts && opts.y !== undefined) t.y = opts.y;
  if (opts && opts.fixed) {
    t.textAutoResize = 'NONE';
    t.resize(opts.fixed[0], opts.fixed[1]);
  }
  return t;
}

function makeRect(x, y, w, h, color, opts) {
  var r = figma.createRectangle();
  r.x = x; r.y = y;
  r.resize(Math.max(w, 1), Math.max(h, 1));
  r.fills = [{ type: 'SOLID', color: color }];
  if (opts && opts.radius) r.cornerRadius = opts.radius;
  if (opts && opts.name) r.name = opts.name;
  return r;
}

// ── ─────────────────────────────────────────────────────────────────────────
//  SITEMAP GENERATOR
// ────────────────────────────────────────────────────────────────────────────

var SM_NODE_W    = 200;
var SM_NODE_H    = 124;
var SM_H_GAP     = 28;
var SM_V_GAP     = 64;
var SM_PADDING   = 60;
var SM_HEADER_H  = 108;
var SM_HEADER_GAP = 20;
var SM_V_OFFSET  = SM_PADDING + SM_HEADER_H + SM_HEADER_GAP;
var SM_LINE_W    = 1.5;
var SM_LINE_CLR  = { r: 0.78, g: 0.78, b: 0.78 };

var SM_COLORS = {
  'Static|AI Generated':    { bg: { r: 0.169, g: 0.659, b: 0.643 }, dark: { r: 0.133, g: 0.522, b: 0.506 } },
  'Static|Client Provided': { bg: { r: 0.788, g: 0.663, b: 0.431 }, dark: { r: 0.624, g: 0.525, b: 0.341 } },
  'CMS|AI Generated':       { bg: { r: 0.290, g: 0.435, b: 0.647 }, dark: { r: 0.231, g: 0.345, b: 0.514 } },
  'CMS|Client Provided':    { bg: { r: 0.878, g: 0.482, b: 0.329 }, dark: { r: 0.698, g: 0.380, b: 0.259 } },
};
var SM_FALLBACK = { bg: { r: 0.5, g: 0.5, b: 0.5 }, dark: { r: 0.4, g: 0.4, b: 0.4 } };

function smSlugDepth(slug) {
  if (slug === '/') return 0;
  return slug.split('/').filter(Boolean).length;
}
function smSlugParent(slug) {
  if (slug === '/' || smSlugDepth(slug) <= 1) return '/';
  var parts = slug.split('/').filter(Boolean); parts.pop();
  return '/' + parts.join('/');
}
function smBuildTree(pages) {
  var map = {};
  for (var i = 0; i < pages.length; i++) map[pages[i].slug] = Object.assign({}, pages[i], { children: [] });
  if (!map['/']) map['/'] = { slug: '/', title: 'Home', page_type: 'Static', content_mode: 'AI Generated', order: 0, purpose: '', children: [] };
  var orphans = [];
  var slugs = Object.keys(map);
  for (var j = 0; j < slugs.length; j++) {
    if (slugs[j] === '/') continue;
    var par = smSlugParent(slugs[j]);
    if (map[par]) map[par].children.push(map[slugs[j]]);
    else orphans.push(map[slugs[j]]);
  }
  for (var k = 0; k < orphans.length; k++) map['/'].children.push(orphans[k]);
  smSortNode(map['/']); return map['/'];
}
function smSortNode(n) {
  n.children.sort(function(a,b){ return (a.order||0)-(b.order||0); });
  for (var i = 0; i < n.children.length; i++) smSortNode(n.children[i]);
}
function smCalcSW(n) {
  if (!n.children.length) { n._sw = SM_NODE_W; return; }
  for (var i=0;i<n.children.length;i++) smCalcSW(n.children[i]);
  var tot = 0; for (var j=0;j<n.children.length;j++) tot += n.children[j]._sw;
  n._sw = Math.max(SM_NODE_W, tot + SM_H_GAP*(n.children.length-1));
}
function smAssignPos(n, x, depth) {
  n._x = x + (n._sw - SM_NODE_W)/2;
  n._y = depth * (SM_NODE_H + SM_V_GAP);
  var cx = x;
  for (var i=0;i<n.children.length;i++) { smAssignPos(n.children[i], cx, depth+1); cx += n.children[i]._sw + SM_H_GAP; }
}
function smCollect(n, out) { if (!out) out=[]; out.push(n); for (var i=0;i<n.children.length;i++) smCollect(n.children[i],out); return out; }

function smAddRect(x,y,w,h,container) {
  var r = makeRect(x,y,w,h,SM_LINE_CLR); r.name='Connector'; container.appendChild(r); return r;
}
function smDrawConnectors(n, container) {
  if (!n.children.length) return;
  var px = n._x + SM_NODE_W/2 + SM_PADDING;
  var py = n._y + SM_NODE_H + SM_V_OFFSET;
  var midY = py + SM_V_GAP/2;
  smAddRect(px-SM_LINE_W/2, py, SM_LINE_W, SM_V_GAP/2, container);
  if (n.children.length === 1) {
    var cx = n.children[0]._x + SM_NODE_W/2 + SM_PADDING;
    smAddRect(cx-SM_LINE_W/2, midY, SM_LINE_W, SM_V_GAP/2, container);
  } else {
    var lx = n.children[0]._x + SM_NODE_W/2 + SM_PADDING;
    var rx = n.children[n.children.length-1]._x + SM_NODE_W/2 + SM_PADDING;
    smAddRect(lx, midY-SM_LINE_W/2, rx-lx, SM_LINE_W, container);
    for (var i=0;i<n.children.length;i++) { var ccx=n.children[i]._x+SM_NODE_W/2+SM_PADDING; smAddRect(ccx-SM_LINE_W/2,midY,SM_LINE_W,SM_V_GAP/2,container); }
  }
  for (var j=0;j<n.children.length;j++) smDrawConnectors(n.children[j],container);
}

async function smCreateNode(node, container, fontR, fontSB) {
  var key = node.page_type+'|'+node.content_mode;
  var clr = SM_COLORS[key] || SM_FALLBACK;
  var frame = figma.createFrame();
  frame.name = node.title; frame.resize(SM_NODE_W, SM_NODE_H);
  frame.x = node._x+SM_PADDING; frame.y = node._y+SM_V_OFFSET;
  frame.cornerRadius = 8; frame.fills = [{type:'SOLID',color:clr.bg}]; frame.clipsContent = true;

  var typeLabel = node.page_type==='CMS'?'CMS':'Static';
  var modeLabel = node.content_mode==='AI Generated'?'AI Copy':'Client Copy';

  var t1 = makeText(node.title, fontSB, 11, WHITE, {x:12,y:9,maxChars:26});
  frame.appendChild(t1);
  var t2 = makeText(node.slug||'', fontR, 9, WHITE, {x:12,y:24,opacity:0.6,maxChars:30});
  frame.appendChild(t2);
  if (node.purpose) {
    var t3 = makeText(node.purpose, fontR, 9, WHITE, {x:12,y:38,opacity:0.8,fixed:[SM_NODE_W-24,64]});
    frame.appendChild(t3);
  }
  var bar = makeRect(0, SM_NODE_H-22, SM_NODE_W, 22, clr.dark);
  frame.appendChild(bar);
  var t4 = makeText(typeLabel+'  ·  '+modeLabel, fontR, 9, WHITE, {x:10,y:SM_NODE_H-16,opacity:0.9});
  frame.appendChild(t4);
  container.appendChild(frame);
}

async function smAddHeader(container, clientName, generatedAt, stats, fontR, fontSB, contentW) {
  var w = Math.max(contentW, 600);
  var frame = figma.createFrame();
  frame.name = 'Header'; frame.resize(w, SM_HEADER_H);
  frame.x = SM_PADDING; frame.y = SM_PADDING;
  frame.cornerRadius = 12; frame.fills = [{type:'SOLID',color:WHITE}];

  var t1 = makeText(clientName+' — Sitemap', fontSB, 20, DARK, {x:24,y:18});
  frame.appendChild(t1);
  var t2 = makeText('AI-Generated  ·  '+generatedAt, fontR, 11, GRAY, {x:24,y:44});
  frame.appendChild(t2);
  var div = makeRect(24, 62, w-48, 1, LIGHT_GRAY);
  frame.appendChild(div);

  var statItems = [
    {label:'Total',value:String(stats.total)},{label:'Static',value:String(stats.static)},
    {label:'CMS',value:String(stats.cms)},{label:'AI Copy',value:String(stats.ai)},
    {label:'Client Copy',value:String(stats.client)}
  ];
  var sx = 24;
  for (var i=0;i<statItems.length;i++) {
    var num = makeText(statItems[i].value, fontSB, 14, DARK, {x:sx,y:70});
    frame.appendChild(num);
    var lbl = makeText(statItems[i].label, fontR, 9, GRAY, {x:sx,y:86});
    frame.appendChild(lbl);
    sx += 90;
  }
  var legItems = [
    {label:'Static / AI',   color: SM_COLORS['Static|AI Generated'].bg},
    {label:'Static / Client',color:SM_COLORS['Static|Client Provided'].bg},
    {label:'CMS / AI',      color:SM_COLORS['CMS|AI Generated'].bg},
    {label:'CMS / Client',  color:SM_COLORS['CMS|Client Provided'].bg},
  ];
  var lx = w-380;
  for (var j=0;j<legItems.length;j++) {
    var dot = figma.createEllipse(); dot.resize(10,10); dot.x=lx; dot.y=72;
    dot.fills=[{type:'SOLID',color:legItems[j].color}]; frame.appendChild(dot);
    var lt = makeText(legItems[j].label, fontR, 10, GRAY, {x:lx+15,y:70});
    frame.appendChild(lt); lx+=95;
  }
  container.appendChild(frame);
}

async function generateSitemap(data) {
  var pages = data.pages||[];
  if (!pages.length) { figma.notify('No pages found.',{error:true}); return; }

  await loadCoreFonts();
  var fontR  = {family:'Inter',style:'Regular'};
  var fontSB = {family:'Inter',style:'Semi Bold'};

  var root = smBuildTree(pages);
  smCalcSW(root); smAssignPos(root,0,0);
  var all = smCollect(root,[]);

  var stats = {total:all.length,static:0,cms:0,ai:0,client:0};
  for (var i=0;i<all.length;i++) {
    if (all[i].page_type==='Static') stats.static++; else stats.cms++;
    if (all[i].content_mode==='AI Generated') stats.ai++; else stats.client++;
  }
  var maxX=0,maxY=0;
  for (var j=0;j<all.length;j++) {
    if (all[j]._x+SM_NODE_W>maxX) maxX=all[j]._x+SM_NODE_W;
    if (all[j]._y+SM_NODE_H>maxY) maxY=all[j]._y+SM_NODE_H;
  }
  var W = maxX+SM_PADDING*2, H = SM_V_OFFSET+maxY+SM_PADDING;

  await getOrReplacePage('🗺 Sitemap');
  var container = figma.createFrame();
  container.name = (data.client||'Client')+' — Sitemap';
  container.resize(W,H); container.fills=[{type:'SOLID',color:BG}];
  container.x = figma.viewport.center.x-W/2;
  container.y = figma.viewport.center.y-H/2;

  await smAddHeader(container,data.client||'Client',data.generated_at||'',stats,fontR,fontSB,maxX);
  smDrawConnectors(root,container);
  for (var k=0;k<all.length;k++) await smCreateNode(all[k],container,fontR,fontSB);

  figma.currentPage.appendChild(container);
  figma.currentPage.selection=[container];
  figma.viewport.scrollAndZoomIntoView([container]);
  figma.notify('✓ Sitemap page updated — '+all.length+' pages');
  figma.ui.postMessage({type:'done',key:'sitemap',detail:all.length+' pages'});
}

// ── ─────────────────────────────────────────────────────────────────────────
//  MOOD BOARD GENERATOR
// ────────────────────────────────────────────────────────────────────────────

var MB_CARD_W      = 268;
var MB_CARD_GAP    = 24;
var MB_PADDING     = 60;
var MB_HEADER_H    = 88;
var MB_HEADER_GAP  = 24;
var MB_CARD_HDR_H  = 72;
var MB_SWATCH_H    = 52;
var MB_CONTENT_PAD = 16;

var MB_OPTION_COLORS = ['#2BA8A4','#4A6FA5','#C9A96E','#3D2B5E'];

async function generateMoodBoards(data) {
  var variations = data.variations||[];
  if (!variations.length) { figma.notify('No variations found.',{error:true}); return; }

  await loadCoreFonts();
  var fontR  = {family:'Inter',style:'Regular'};
  var fontM  = {family:'Inter',style:'Medium'};
  var fontSB = {family:'Inter',style:'Semi Bold'};
  var fontB  = {family:'Inter',style:'Bold'};

  await getOrReplacePage('🎨 Mood Boards');

  var totalW = MB_PADDING + variations.length*(MB_CARD_W+MB_CARD_GAP) - MB_CARD_GAP + MB_PADDING;

  // Build each card to get its height, then set canvas height
  var cardH = mbCardHeight();
  var totalH = MB_PADDING + MB_HEADER_H + MB_HEADER_GAP + cardH + MB_PADDING;

  // Page bg
  var pageBg = figma.createRectangle();
  pageBg.name = 'Background';
  pageBg.resize(totalW, totalH);
  pageBg.x = 0; pageBg.y = 0;
  pageBg.fills = [{type:'SOLID',color:BG}];
  figma.currentPage.appendChild(pageBg);

  // Header
  await mbAddHeader(data.client||'Client', data.generated_at||'', variations.length, fontR, fontSB, totalW, MB_PADDING);

  // Cards
  var cardY = MB_PADDING + MB_HEADER_H + MB_HEADER_GAP;
  for (var i=0; i<variations.length; i++) {
    var cardX = MB_PADDING + i*(MB_CARD_W + MB_CARD_GAP);
    await mbCreateCard(variations[i], cardX, cardY, i, fontR, fontM, fontSB, fontB);
  }

  figma.viewport.scrollAndZoomIntoView(figma.currentPage.children);
  figma.notify('✓ Mood Boards page updated — '+variations.length+' variations');
  figma.ui.postMessage({type:'done',key:'moodboard',detail:variations.length+' variations'});
}

function mbCardHeight() {
  // Fixed card height: header + swatches + content
  return MB_CARD_HDR_H + MB_SWATCH_H + 280;
}

async function mbAddHeader(clientName, generatedAt, count, fontR, fontSB, totalW, padX) {
  var frame = figma.createFrame();
  frame.name = 'Header';
  frame.resize(totalW - padX*2, MB_HEADER_H);
  frame.x = padX; frame.y = MB_PADDING;
  frame.cornerRadius = 12;
  frame.fills = [{type:'SOLID',color:WHITE}];

  var t1 = makeText(clientName+' — Mood Boards', fontSB, 20, DARK, {x:24,y:16});
  frame.appendChild(t1);
  var t2 = makeText(count+' creative directions · Select one to advance to brand guidelines', fontR, 11, GRAY, {x:24,y:42});
  frame.appendChild(t2);
  var t3 = makeText('Generated by RxMedia AI Pipeline  ·  '+generatedAt, fontR, 10, GRAY, {x:24,y:58,opacity:0.6});
  frame.appendChild(t3);
  figma.currentPage.appendChild(frame);
}

async function mbCreateCard(v, x, y, index, fontR, fontM, fontSB, fontB) {
  var cardH = mbCardHeight();
  var primaryHex = (v.colors && v.colors.length > 0 && v.colors[0].hex) ? v.colors[0].hex : MB_OPTION_COLORS[index % MB_OPTION_COLORS.length];
  var primaryRgb = hexToRgb(primaryHex);
  var isRecommended = v.status === 'Pending Review' || v.status === 'Approved';

  // Card frame
  var card = figma.createFrame();
  card.name = v.option || ('Option '+(index+1));
  card.resize(MB_CARD_W, cardH);
  card.x = x; card.y = y;
  card.cornerRadius = 12;
  card.fills = [{type:'SOLID',color:WHITE}];
  card.clipsContent = true;

  // ── Card header (colored) ──
  var hdr = makeRect(0, 0, MB_CARD_W, MB_CARD_HDR_H, primaryRgb, {name:'CardHeader'});
  card.appendChild(hdr);

  var optLabel = makeText((v.option||'Option').toUpperCase(), {family:'Inter',style:'Medium'}, 9, WHITE, {x:MB_CONTENT_PAD,y:12,opacity:0.8});
  card.appendChild(optLabel);

  var conceptName = makeText(v.concept_name||v.option||'', fontSB, 16, WHITE, {x:MB_CONTENT_PAD,y:26,maxChars:28});
  card.appendChild(conceptName);

  if (isRecommended) {
    var badge = makeRect(MB_CARD_W-90, 20, 74, 22, darken(primaryRgb, 0.12), {radius:11});
    badge.name = 'RecommendedBadge';
    card.appendChild(badge);
    var badgeT = makeText('★ Top Pick', {family:'Inter',style:'Medium'}, 9, WHITE, {x:MB_CARD_W-80,y:27});
    card.appendChild(badgeT);
  }

  // ── Color swatches ──
  var swY = MB_CARD_HDR_H;
  var colors = v.colors||[];
  var swatchCount = Math.min(colors.length, 5);
  if (swatchCount > 0) {
    var swW = MB_CARD_W / swatchCount;
    for (var i=0; i<swatchCount; i++) {
      var sw = makeRect(i*swW, swY, swW, MB_SWATCH_H, hexToRgb(colors[i].hex), {name:colors[i].hex});
      card.appendChild(sw);
      // Hex label at bottom of swatch
      var hexT = makeText(colors[i].hex, fontR, 7, WHITE, {x:i*swW+4, y:swY+MB_SWATCH_H-14, opacity:0.85});
      card.appendChild(hexT);
    }
  }

  // ── Content area ──
  var cy = MB_CARD_HDR_H + MB_SWATCH_H + 16;

  // Sample headline
  var hl = makeText(v.headline||'Expert care. Your terms.', fontSB, 13, primaryRgb, {x:MB_CONTENT_PAD,y:cy,fixed:[MB_CARD_W-MB_CONTENT_PAD*2,40]});
  card.appendChild(hl);
  cy += 48;

  // Typography
  var fontStr = 'Heading: '+(v.primary_font||'Quicksand')+'  ·  Body: '+(v.secondary_font||'Inter');
  var fontT = makeText(fontStr, fontR, 9, GRAY, {x:MB_CONTENT_PAD,y:cy});
  card.appendChild(fontT);
  cy += 22;

  // Divider
  var div1 = makeRect(MB_CONTENT_PAD, cy, MB_CARD_W-MB_CONTENT_PAD*2, 1, LIGHT_GRAY);
  card.appendChild(div1);
  cy += 12;

  // Scores
  var scores = v.scores||{};
  var scoreKeys = Object.keys(scores);
  for (var si=0; si<Math.min(scoreKeys.length, 5); si++) {
    var sk = scoreKeys[si];
    var sv = scores[sk]||7;
    var barW = MB_CARD_W - MB_CONTENT_PAD*2 - 30;
    var filledW = Math.round((sv/10)*barW);

    var scoreLbl = makeText(sk, fontR, 8, GRAY, {x:MB_CONTENT_PAD,y:cy});
    card.appendChild(scoreLbl);
    var scoreNum = makeText(String(sv), {family:'Inter',style:'Medium'}, 8, primaryRgb, {x:MB_CARD_W-MB_CONTENT_PAD-12,y:cy});
    card.appendChild(scoreNum);

    var barBg = makeRect(MB_CONTENT_PAD, cy+12, barW, 4, LIGHT_GRAY, {radius:2});
    card.appendChild(barBg);
    if (filledW > 0) {
      var barFill = makeRect(MB_CONTENT_PAD, cy+12, filledW, 4, primaryRgb, {radius:2});
      card.appendChild(barFill);
    }
    cy += 26;
  }

  cy += 4;
  var div2 = makeRect(MB_CONTENT_PAD, cy, MB_CARD_W-MB_CONTENT_PAD*2, 1, LIGHT_GRAY);
  card.appendChild(div2);
  cy += 12;

  // Strengths
  var strengths = v.strengths||[];
  for (var sti=0; sti<Math.min(strengths.length,3); sti++) {
    var st = makeText('✓  '+strengths[sti], fontR, 9, {r:0.2,g:0.6,b:0.4}, {x:MB_CONTENT_PAD,y:cy,maxChars:38});
    card.appendChild(st);
    cy += 16;
  }
  // Risks
  var risks = v.risks||[];
  for (var ri=0; ri<Math.min(risks.length,2); ri++) {
    var rsk = makeText('⚠  '+risks[ri], fontR, 9, {r:0.8,g:0.5,b:0.2}, {x:MB_CONTENT_PAD,y:cy,maxChars:38});
    card.appendChild(rsk);
    cy += 16;
  }

  figma.currentPage.appendChild(card);
}

// ── ─────────────────────────────────────────────────────────────────────────
//  BRAND GUIDELINES GENERATOR
// ────────────────────────────────────────────────────────────────────────────

var BG_PAD = 60;
var BG_COL_W = 900;

async function generateBrandGuidelines(data) {
  var colors    = data.colors||[];
  var fontR     = {family:'Inter',style:'Regular'};
  var fontM     = {family:'Inter',style:'Medium'};
  var fontSB    = {family:'Inter',style:'Semi Bold'};
  var fontB     = {family:'Inter',style:'Bold'};

  await loadCoreFonts();

  // Try to load brand fonts
  var headingFont = await loadFont(data.typography && data.typography.heading ? data.typography.heading.family : 'Inter', 'Regular');
  var bodyFont    = {family:'Inter',style:'Regular'};

  await getOrReplacePage('🎯 Brand Guidelines');

  var pageW = BG_PAD*2 + BG_COL_W;
  var curY  = BG_PAD;

  // Running list of elements to measure total height
  var els = [];

  function place(el, x, y) { el.x = x; el.y = y; figma.currentPage.appendChild(el); return el; }

  // ── Title block ──
  var titleFrame = figma.createFrame();
  titleFrame.name = 'TitleBlock';
  titleFrame.resize(BG_COL_W, 80);
  titleFrame.x = BG_PAD; titleFrame.y = curY;
  titleFrame.cornerRadius = 12;
  titleFrame.fills = [{type:'SOLID',color:WHITE}];
  var t1 = makeText((data.client||'Client')+' — Brand Guidelines', fontB, 22, DARK, {x:24,y:16});
  titleFrame.appendChild(t1);
  var t2 = makeText('Derived from approved mood board  ·  '+data.approved_variation+'  ·  '+data.generated_at, fontR, 11, GRAY, {x:24,y:46});
  titleFrame.appendChild(t2);
  figma.currentPage.appendChild(titleFrame);
  curY += 80 + 32;

  // ── Color Palette ──
  var secLabel1 = makeText('COLOR PALETTE', fontSB, 11, GRAY, {x:BG_PAD,y:curY,opacity:0.7});
  secLabel1.letterSpacing = {value:1.2,unit:'PIXELS'};
  figma.currentPage.appendChild(secLabel1);
  curY += 24;

  var swatchW = 160, swatchH = 100, swatchGap = 16;
  for (var ci=0; ci<colors.length; ci++) {
    var cx = BG_PAD + ci*(swatchW+swatchGap);
    var swFrame = figma.createFrame();
    swFrame.name = colors[ci].name||colors[ci].hex;
    swFrame.resize(swatchW, swatchH+44);
    swFrame.x = cx; swFrame.y = curY;
    swFrame.cornerRadius = 10;
    swFrame.fills = [{type:'SOLID',color:WHITE}];
    swFrame.clipsContent = true;

    var swColor = makeRect(0, 0, swatchW, swatchH, hexToRgb(colors[ci].hex));
    swFrame.appendChild(swColor);
    var swName = makeText(colors[ci].name||colors[ci].role||'', fontM, 11, DARK, {x:12,y:swatchH+10,maxChars:22});
    swFrame.appendChild(swName);
    var swHex = makeText(colors[ci].hex, fontR, 10, GRAY, {x:12,y:swatchH+26});
    swFrame.appendChild(swHex);
    figma.currentPage.appendChild(swFrame);
  }
  curY += swatchH + 44 + 36;

  // ── Typography ──
  var secLabel2 = makeText('TYPOGRAPHY', fontSB, 11, GRAY, {x:BG_PAD,y:curY,opacity:0.7});
  secLabel2.letterSpacing = {value:1.2,unit:'PIXELS'};
  figma.currentPage.appendChild(secLabel2);
  curY += 24;

  var fonts = [
    { role: 'Heading Typeface', family: (data.typography&&data.typography.heading)?data.typography.heading.family:'Quicksand', sample: 'Aa Bb Cc — The quick brown fox', renderFont: headingFont },
    { role: 'Body Typeface',    family: (data.typography&&data.typography.body)?data.typography.body.family:'Inter',      sample: 'The quick brown fox jumps over the lazy dog. 0123456789', renderFont: bodyFont },
  ];

  for (var fi=0; fi<fonts.length; fi++) {
    var fData = fonts[fi];
    var fFrame = figma.createFrame();
    fFrame.name = fData.role;
    fFrame.resize(BG_COL_W, 88);
    fFrame.x = BG_PAD; fFrame.y = curY;
    fFrame.cornerRadius = 10;
    fFrame.fills = [{type:'SOLID',color:WHITE}];

    var fLabel = makeText(fData.role.toUpperCase(), {family:'Inter',style:'Medium'}, 9, GRAY, {x:20,y:12});
    fFrame.appendChild(fLabel);
    var fName = makeText(fData.family, fontSB, 14, DARK, {x:20,y:26});
    fFrame.appendChild(fName);
    var fSample = makeText(fData.sample, fData.renderFont, 13, GRAY, {x:20,y:50,fixed:[BG_COL_W-40,28]});
    fFrame.appendChild(fSample);
    figma.currentPage.appendChild(fFrame);
    curY += 88 + 12;
  }
  curY += 24;

  // ── Tone of Voice ──
  var tone = data.tone||[];
  if (tone.length) {
    var secLabel3 = makeText('TONE OF VOICE', fontSB, 11, GRAY, {x:BG_PAD,y:curY,opacity:0.7});
    secLabel3.letterSpacing = {value:1.2,unit:'PIXELS'};
    figma.currentPage.appendChild(secLabel3);
    curY += 24;

    var toneFrame = figma.createFrame();
    toneFrame.name = 'ToneOfVoice';
    toneFrame.resize(BG_COL_W, 72);
    toneFrame.x = BG_PAD; toneFrame.y = curY;
    toneFrame.cornerRadius = 10;
    toneFrame.fills = [{type:'SOLID',color:WHITE}];

    var toneStr = tone.join('  ·  ');
    var toneT = makeText(toneStr, fontSB, 14, DARK, {x:24,y:16,fixed:[BG_COL_W-48,30]});
    toneFrame.appendChild(toneT);
    if (data.positioning) {
      var posT = makeText(data.positioning, fontR, 11, GRAY, {x:24,y:44,fixed:[BG_COL_W-48,20]});
      toneFrame.appendChild(posT);
    }
    figma.currentPage.appendChild(toneFrame);
    curY += 72 + 24;
  }

  // Zoom to fit
  figma.viewport.scrollAndZoomIntoView(figma.currentPage.children);
  figma.notify('✓ Brand Guidelines page updated');
  figma.ui.postMessage({type:'done',key:'brand',detail:'brand guidelines'});
}

// ── Message handler ───────────────────────────────────────────────────────────
figma.ui.onmessage = async function(msg) {
  var key = msg.type.replace('generate-','');
  try {
    if (msg.type === 'generate-sitemap') {
      await generateSitemap(msg.data);
    } else if (msg.type === 'generate-moodboard') {
      await generateMoodBoards(msg.data);
    } else if (msg.type === 'generate-brand') {
      await generateBrandGuidelines(msg.data);
    } else if (msg.type === 'cancel') {
      figma.closePlugin();
    }
  } catch(err) {
    var errMsg = (err && err.message) ? err.message : String(err);
    figma.notify('Error: '+errMsg, {error:true});
    figma.ui.postMessage({type:'error', key:key, message:errMsg});
  }
};
