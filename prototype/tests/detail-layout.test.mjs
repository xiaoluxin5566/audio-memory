import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const appSource = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');

test('detail page removes continuation questions and conversation history', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.equal(detail.indexOf('className="qa-section"'), -1);
  assert.equal(detail.indexOf('className="ask-section"'), -1);
});

test('detail does not expose the question API', () => {
  assert.doesNotMatch(appSource, /api\.askCard/);
});

test('feed and detail copy use readable full-screen type sizes', () => {
  assert.match(styles, /\.result-card h3\{font-size:16px/);
  assert.match(styles, /\.result-card p\{font-size:13px/);
  assert.match(styles, /\.detail-section p\{font-size:14px/);
  assert.match(styles, /\.detail-section li\{[^}]*font-size:13px/);
  assert.match(styles, /\.detail-header h1\{[^}]*font-size:32px/);
});

test('detail renders common blocks rather than strict scene payload keys', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /card\.detailSections\.map/)
  assert.match(detail, /MeetingDetailSection/)
  assert.match(detail, /meeting-quote|meeting-argument|meeting-recommendation/)
  assert.doesNotMatch(detail, /inferred_title_hint|evidence_segment_ids|generation_reason|finding_id|case_id/)
});

test('detail exposes bounded evidence playback without transcript text', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /<EvidencePlayback evidence=\{card\.evidence\}/)
  assert.match(appSource, /<summary>回听证据 · \{evidence\.length\} 段<\/summary>/)
  assert.match(appSource, /<audio key=\{source\} controls preload="metadata" src=\{source\}/)
  assert.match(appSource, /#t=\$\{\(active\.startMs \/ 1000\)\.toFixed\(3\)\},/)
  assert.doesNotMatch(appSource, /active\.text|evidence\.text/)
  assert.match(styles, /\.evidence-playback\{/)
});

test('analysis detail permanently suppresses evidence playback', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /card\.sceneId !== 'analysis' && card\.showEvidencePlayback !== false/)
});

test('single report renders structured markdown and runtime metrics without old three-part labels', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /<MarkdownReport markdown=\{card\.reportMarkdown\}/);
  assert.match(appSource, /DeepSeek 正在阅读全文并生成报告/);
  assert.match(appSource, /生成全天报告/);
  assert.doesNotMatch(appSource, /自主分析、隐藏画像与发布/);
  assert.match(appSource, /block\.kind === 'image'/);
  assert.match(appSource, /className="report-image"/);
  assert.match(detail, /<RuntimeMetrics metrics=\{card\.runtimeMetrics\}/);
  const markdownRenderer = appSource.slice(
    appSource.indexOf('function MarkdownReport'),
    appSource.indexOf('function RuntimeMetrics'),
  );
  assert.doesNotMatch(markdownRenderer, /01 · 场景还原|02 · 分析点评|03 · 下一步建议/);
  assert.match(styles, /\.markdown-report/);
  assert.match(styles, /\.runtime-metrics/);
  assert.match(styles, /\.report-image/);
  assert.match(detail, /annotations=\{card\.reportAnnotations\}/);
  assert.match(appSource, /data-annotation-mode=\{annotations \? 'model' : 'markdown'\}/);
});

test('single report renders a grounded event table before the complete markdown', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function History'),
  );

  assert.match(detail, /buildReportEventMap\(card\.reportMarkdown\)/);
  assert.match(detail, /<ReportEventMap presentation=\{presentation\}/);
  assert.match(detail, /className="report-event-map"/);
  assert.match(detail, /<table className="report-event-table"/);
  assert.match(detail, /<th>阶段<\/th><th>发生的事<\/th><th>对应的改进<\/th>/);
  assert.doesNotMatch(detail, /report-event-connector/);
});

test('structured report renders semantic content directly without markdown inference', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function RuntimeMetrics'),
  );
  const renderer = appSource.slice(
    appSource.indexOf('function StructuredReport'),
    appSource.indexOf('function MarkdownReport'),
  );

  assert.match(detail, /card\.reportDocument \? <StructuredReport document=\{card\.reportDocument\}/);
  assert.match(detail, /card\.reportDocument \? null : buildReportEventMap\(card\.reportMarkdown\)/);
  assert.match(renderer, /<table className="report-event-table"/);
  assert.match(renderer, /sectionIndex \+ 1/);
  assert.match(renderer, /subsectionIndex \+ 1/);
  assert.match(renderer, /structured-source-quote/);
  assert.match(renderer, /structured-suggested-wording/);
  assert.match(renderer, /“\{block\.text\}”/);
  assert.match(renderer, /<table className="analysis-matrix"/);
});

test('single report numbers major and minor headings by their real hierarchy', () => {
  const renderer = appSource.slice(
    appSource.indexOf('function AnalysisBlocks'),
    appSource.indexOf('function EvidencePlayback'),
  );

  assert.match(renderer, /majorSectionNumber/);
  assert.match(renderer, /minorSectionNumber/);
  assert.match(renderer, /block\.level === 2/);
  assert.match(renderer, /className="analysis-section-heading"/);
  assert.match(renderer, /className="analysis-subheading"/);
});

test('report lists use plain semantic list markup instead of card layouts', () => {
  const renderer = appSource.slice(
    appSource.indexOf('function AnalysisBlocks'),
    appSource.indexOf('function EvidencePlayback'),
  );

  assert.match(renderer, /<ul className="analysis-key-points"/);
  assert.match(renderer, /<ol className="analysis-insight-grid"/);
  assert.match(renderer, /<ol className="analysis-timeline"/);
  assert.doesNotMatch(renderer, /className="analysis-insight-grid"[^;]+<article/);
  assert.doesNotMatch(styles, /\.analysis-insight-grid article\{/);
  assert.doesNotMatch(styles, /\.analysis-key-points li\{[^}]*border:/);
});

test('single report keeps only basic document typography and tables', () => {
  const metrics = appSource.slice(
    appSource.indexOf('function RuntimeMetrics'),
    appSource.indexOf('function ExternalSources'),
  );

  assert.match(metrics, /<table className="runtime-metrics-table"/);
  assert.doesNotMatch(metrics, /<dl>|<dt>|<dd>/);
  assert.doesNotMatch(styles, /\.runtime-metrics\{[^}]*background:/);
  assert.doesNotMatch(styles, /\.runtime-metrics\{[^}]*border-radius:/);
  assert.doesNotMatch(styles, /\.analysis-matrix-wrap\{[^}]*border-radius:/);
  assert.doesNotMatch(styles, /\.report-event-map-intro>span/);
});

test('single report renders source quotes with Chinese quotation marks', () => {
  const renderer = appSource.slice(
    appSource.indexOf('function AnalysisBlocks'),
    appSource.indexOf('function EvidencePlayback'),
  );

  assert.match(renderer, /block\.kind === 'quote'/);
  assert.match(renderer, /<blockquote className="analysis-quote"/);
  assert.match(renderer, /“\{block\.text\}”/);
});

test('single report uses one restrained typography scale', () => {
  assert.match(styles, /\.markdown-report\{[^}]*--report-body-size:15px/);
  assert.match(styles, /--report-body-leading:1\.85/);
  assert.match(styles, /\.markdown-report \.analysis-section-heading\{[^}]*font-size:24px/);
  assert.match(styles, /\.markdown-report \.analysis-subheading\{[^}]*font-size:18px/);
  assert.match(styles, /\.markdown-report \.analysis-paragraph[^}]*font-size:var\(--report-body-size\)/);
});

test('single report summary and detail share one width without repeated core conclusion', () => {
  const detail = appSource.slice(
    appSource.indexOf('function CardDetail'),
    appSource.indexOf('function RuntimeMetrics'),
  );

  assert.match(detail, /<MarkdownReport markdown=\{card\.reportMarkdown\} annotations=\{card\.reportAnnotations\} omitCoreConclusion=\{Boolean\(presentation\)\}/);
  assert.match(detail, /function omitMarkdownSection/);
  assert.match(detail, /omitCoreConclusion \? omitMarkdownSection\(body, '核心结论'\) : body/);
  assert.doesNotMatch(detail, />完整报告</);
  assert.match(styles, /\.report-event-map,.markdown-report,.runtime-metrics\{max-width:960px;margin-inline:auto\}/);
  assert.doesNotMatch(styles, /\.markdown-report\{[^}]*max-width:860px/);
  assert.match(styles, /\.markdown-report \.analysis-blocks>\.analysis-section-heading:first-child\{[^}]*margin-top:24px/);
});

test('major report sections use restrained dividers without card decoration', () => {
  assert.match(styles, /\.markdown-report \.analysis-section-heading\{[^}]*border-top:1px solid #dfe4ea/);
  assert.match(styles, /\.markdown-report \.analysis-blocks>\.analysis-section-heading:first-child\{[^}]*border-top:0/);
  assert.doesNotMatch(styles, /\.markdown-report \.analysis-section-heading\{[^}]*(?:background|border-radius|box-shadow):/);
});

test('content inside each major section uses a compact vertical rhythm', () => {
  assert.match(styles, /\.markdown-report \.analysis-blocks\{gap:8px\}/);
  assert.match(styles, /\.markdown-report \.analysis-subheading\{[^}]*margin:18px 0 6px/);
  assert.match(styles, /\.markdown-report \.analysis-key-points,.markdown-report \.analysis-insight-grid,.markdown-report \.analysis-timeline\{[^}]*margin:0 0 4px/);
  assert.match(styles, /\.markdown-report \.analysis-quote\{margin:2px 0 6px/);
});
