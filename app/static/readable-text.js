// Presentation only: retain source strings for editing, citations and review.
// Infer lists only from an explicit, consecutive sequence with substantial text.
const protectedPattern = /(`{3,})[\s\S]*?(?:\1|$)|(`{1,2})[^\n]*?\2|\$\$[\s\S]*?(?:\$\$|$)|\$[^\n$]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|https?:\/\/\S+/g;
const markerPattern = /(?<![^\s。！？.!?;；:：])(?:(Claim|Statement|Step|Condition|陈述|说法|条件|步骤|观点)\s*(\d{1,2})\s*[:：]|\((\d{1,2})\)|（(\d{1,2})）|(\d{1,2})([.)、])(?=\s))/giu;

function protectedRanges(value) {
  return [...value.matchAll(protectedPattern)].map(match => [match.index, match.index + match[0].length]);
}

function paragraphs(value) {
  const ranges = protectedRanges(value);
  const blocks = [];
  let start = 0;
  for (const match of value.matchAll(/\n[\t ]*\n+/g)) {
    if (ranges.some(([a,b]) => match.index >= a && match.index < b)) continue;
    if (value.slice(start,match.index).trim()) blocks.push(value.slice(start,match.index));
    start = match.index + match[0].length;
  }
  if (value.slice(start).trim()) blocks.push(value.slice(start));
  return blocks;
}

function splitClosingQuestion(value) {
  const ranges = protectedRanges(value);
  // A final explicit question after a full sentence is a separate instruction,
  // not part of the last numbered premise. Leave ambiguous prose untouched.
  const pattern = /[.!?。！？][\t ]+(?=(?:Which|What|How|Why|When|Where|Who|Select|Choose|Identify|Determine|Explain|Calculate|Compare)\b)|[。！？][\t ]*(?=(?:以下|下列|请|哪|如何|为什么|根据))/g;
  for (const match of value.matchAll(pattern)) {
    const offset = match.index + match[0].length;
    if (ranges.some(([a,b]) => offset >= a && offset < b)) continue;
    const tail = value.slice(offset).trim();
    if (/[?？]$/.test(tail) && !/[?？].+/.test(tail)) {
      return [value.slice(0,match.index + 1).trim(),tail];
    }
  }
  return [value];
}

function splitList(value) {
  const ranges = protectedRanges(value);
  const markers = [...value.matchAll(markerPattern)].filter(match => !ranges.some(([a,b]) => match.index >= a && match.index < b));
  if (markers.length < 2) return [{type:'paragraph',text:value}];
  const family = match => match[1]?.toLowerCase() || (match[3] ? 'paren' : match[4] ? 'wide-paren' : match[6]);
  const number = match => Number(match[2] || match[3] || match[4] || match[5]);
  // Do not turn ordinary references to equation (1) and (2) into a list.
  const first = markers[0];
  const introduction = value.slice(0,first.index).trim();
  if (number(first) !== 1 || (introduction && !/[.!?;:。！？；：]$/.test(introduction))) return [{type:'paragraph',text:value}];
  if (markers.some((marker,index) => family(marker) !== family(first) || number(marker) !== index + 1)) return [{type:'paragraph',text:value}];
  if (!first[1] && markers.slice(0,-1).some((marker,index) => {
    const item = value.slice(marker.index + marker[0].length,markers[index + 1].index);
    return !/[.!?;:。！？；：]\s*$/.test(item) && !/\n[\t ]*$/.test(item);
  })) return [{type:'paragraph',text:value}];
  const items = markers.map((marker,index) => ({
    marker:marker[0],
    text:value.slice(marker.index + marker[0].length,markers[index + 1]?.index ?? value.length).trim(),
  }));
  // Short in-sentence enumerations and mathematical tuples remain inline.
  if (items.some(item => !(/\p{Script=Han}/u.test(item.text) ? item.text.length >= 6 : item.text.split(/\s+/).length >= 3))) return [{type:'paragraph',text:value}];
  const [last,closing] = splitClosingQuestion(items.at(-1).text);
  items.at(-1).text = last;
  return [...(introduction ? [{type:'paragraph',text:introduction}] : []), {type:'list',items}, ...(closing ? [{type:'paragraph',text:closing}] : [])];
}

export function readableBlocks(value) {
  return paragraphs(String(value ?? '').replace(/\r\n?/g,'\n')).flatMap(splitList);
}

// Callers supply the existing escaping/citation renderer; never parse model HTML.
export function renderReadableText(value, renderText) {
  return readableBlocks(value).map(block => block.type === 'list'
    ? `<ol class="material-statements" role="list">${block.items.map(item => `<li><span class="statement-marker">${renderText(item.marker)}</span><div class="statement-text">${renderText(item.text)}</div></li>`).join('')}</ol>`
    : `<p>${renderText(block.text)}</p>`).join('');
}
