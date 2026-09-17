import { Devvit, SettingScope } from '@devvit/public-api';

Devvit.configure({
  redditAPI: true,
  http: true,
});

const DEFAULT_AI_RADAR_BASE_URL = 'http://47.250.164.154.nip.io:2050';
const DEFAULT_SHARED_TOKEN = '';
const DEFAULT_POST_LIMIT = 50;
const DEFAULT_COLLECT_CRON = '0 * * * *';
const COLLECT_JOB_NAME = 'collect-reddit-signals';

type WatchItem = {
  source_item_id: number;
  project_key?: string;
  title?: string;
  repo_url?: string;
  aliases?: string[];
};

type RedditSignal = {
  source_item_id: number;
  platform: 'Reddit';
  source_type: 'post_devvit';
  url: string;
  title: string;
  published_at: string;
  engagement: Record<string, unknown>;
  stance: 'unknown';
  quote_or_excerpt: string;
  topic_tags: string[];
  relevance_reason: string;
  project_key?: string;
  subreddit?: string;
  post_id?: string;
  permalink?: string;
  author?: string;
  score?: number;
  num_comments?: number;
  created_utc?: string;
  matched_aliases?: string[];
};

const DEFAULT_SUBREDDITS = [
  'programming',
  'webdev',
  'MachineLearning',
  'LocalLLaMA',
  'OpenAI',
  'ClaudeAI',
  'SideProject',
  'github',
];

Devvit.addSettings([
  {
    type: 'string',
    name: 'aiRadarBaseUrl',
    label: 'AI Radar public base URL',
    helpText: '例如 https://your-domain.example，不要填写 localhost；Devvit 云端无法访问本机 127.0.0.1。',
    scope: SettingScope.App,
    defaultValue: DEFAULT_AI_RADAR_BASE_URL,
  },
  {
    type: 'string',
    name: 'sharedToken',
    label: 'Shared token',
    helpText: '对应后端 REDDIT_DEVVIT_SHARED_TOKEN。留空则不带鉴权头。',
    scope: SettingScope.App,
    isSecret: true,
  },
  {
    type: 'string',
    name: 'subreddits',
    label: 'Subreddits to scan',
    helpText: '逗号分隔，例如 programming,webdev,LocalLLaMA,SideProject',
    scope: SettingScope.Installation,
    defaultValue: DEFAULT_SUBREDDITS.join(','),
  },
  {
    type: 'number',
    name: 'postLimitPerSubreddit',
    label: 'Post limit per subreddit',
    scope: SettingScope.Installation,
    defaultValue: DEFAULT_POST_LIMIT,
  },
]);

function normalizeText(value: unknown): string {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function termsForItem(item: WatchItem): string[] {
  const raw = [
    item.project_key,
    item.title,
    item.repo_url,
    ...(Array.isArray(item.aliases) ? item.aliases : []),
  ];
  const seen = new Set<string>();
  const terms: string[] = [];
  for (const value of raw) {
    const clean = normalizeText(value);
    if (!clean) continue;
    const key = clean.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    terms.push(clean);
  }
  return terms;
}

function matchesItem(postText: string, item: WatchItem): string[] {
  const haystack = postText.toLowerCase();
  return termsForItem(item).filter((term) => haystack.includes(term.toLowerCase()));
}

function permalinkForPost(post: any): string {
  const permalink = normalizeText(post.permalink);
  if (permalink.startsWith('http://') || permalink.startsWith('https://')) return permalink;
  if (permalink) return `https://www.reddit.com${permalink.startsWith('/') ? permalink : `/${permalink}`}`;
  const id = normalizeText(post.id);
  return id ? `https://www.reddit.com/comments/${id}` : 'https://www.reddit.com/';
}

function isoStringForPost(post: any): string {
  if (post.createdAt instanceof Date) return post.createdAt.toISOString();
  const createdAt = normalizeText(post.createdAt);
  if (createdAt) return createdAt;
  const createdUtc = normalizeText(post.createdUtc ?? post.created_utc);
  if (createdUtc) return createdUtc;
  return 'unknown';
}

function signalForPost(post: any, subredditName: string, item: WatchItem, matchedTerms: string[]): RedditSignal {
  const title = normalizeText(post.title) || 'unknown';
  const body = normalizeText(post.body ?? post.selftext);
  const permalink = permalinkForPost(post);
  const author = normalizeText(post.authorName ?? post.author?.name ?? post.author);
  const createdAt = isoStringForPost(post);
  return {
    source_item_id: Number(item.source_item_id),
    platform: 'Reddit',
    source_type: 'post_devvit',
    url: permalink,
    title,
    published_at: createdAt,
    engagement: {
      subreddit: subredditName,
      score: post.score,
      comments: post.numComments ?? post.numberOfComments,
      reddit_id: post.id,
    },
    stance: 'unknown',
    quote_or_excerpt: body || normalizeText(post.url) || title,
    topic_tags: ['reddit', 'devvit', `r_${subredditName.toLowerCase()}`],
    relevance_reason: `Devvit 扫描 r/${subredditName} 命中项目 watchlist 关键词：${matchedTerms.slice(0, 5).join(', ')}`,
    project_key: normalizeText(item.project_key),
    subreddit: subredditName,
    post_id: normalizeText(post.id),
    permalink,
    author,
    score: Number(post.score ?? 0),
    num_comments: Number(post.numComments ?? post.numberOfComments ?? 0),
    created_utc: createdAt,
    matched_aliases: matchedTerms,
  };
}

async function getSetting(context: any, name: string): Promise<string> {
  const value = await context.settings.get(name);
  return normalizeText(value);
}

async function getSettingOrDefault(context: any, name: string, fallback: string): Promise<string> {
  const value = await getSetting(context, name);
  return value || fallback;
}

function parseSubreddits(value: string): string[] {
  return value
    .split(',')
    .map((entry) => normalizeText(entry))
    .filter(Boolean);
}

function authHeaders(sharedToken: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (sharedToken) headers.Authorization = `Bearer ${sharedToken}`;
  return headers;
}

async function fetchWatchlist(baseUrl: string, sharedToken: string): Promise<WatchItem[]> {
  const response = await fetch(`${baseUrl}/api/integrations/reddit-devvit/watchlist`, {
    headers: authHeaders(sharedToken),
  });
  if (!response.ok) throw new Error(`watchlist failed: ${response.status} ${await response.text()}`);
  const payload = await response.json() as { items?: WatchItem[] };
  return Array.isArray(payload.items) ? payload.items : [];
}

async function postSignals(baseUrl: string, sharedToken: string, signals: RedditSignal[]): Promise<void> {
  if (!signals.length) return;
  const response = await fetch(`${baseUrl}/api/integrations/reddit-devvit/signals`, {
    method: 'POST',
    headers: authHeaders(sharedToken),
    body: JSON.stringify({
      source: 'reddit_devvit_collector',
      generated_at: new Date().toISOString(),
      signals,
    }),
  });
  if (!response.ok) throw new Error(`ingest failed: ${response.status} ${await response.text()}`);
}

async function collectRedditSignals(context: any): Promise<number> {
  const rawBaseUrl = await getSettingOrDefault(context, 'aiRadarBaseUrl', DEFAULT_AI_RADAR_BASE_URL);
  const baseUrl = rawBaseUrl.replace(/\/+$/, '');
  const sharedToken = await getSettingOrDefault(context, 'sharedToken', DEFAULT_SHARED_TOKEN);
  const subredditSetting = await getSettingOrDefault(context, 'subreddits', DEFAULT_SUBREDDITS.join(','));
  const subreddits = parseSubreddits(subredditSetting);
  const postLimit = Math.max(1, Math.min(Number(await context.settings.get('postLimitPerSubreddit') ?? DEFAULT_POST_LIMIT), 100));
  const watchlist = await fetchWatchlist(baseUrl, sharedToken);
  const signals: RedditSignal[] = [];

  console.log(`[reddit-devvit] collect start baseUrl=${baseUrl} subreddits=${subreddits.join(',')} watchlist=${watchlist.length} postLimit=${postLimit}`);

  for (const subredditName of subreddits) {
    try {
      const listing = await context.reddit.getNewPosts({ subredditName, limit: postLimit });
      const posts = typeof listing.all === 'function' ? await listing.all() : [];
      console.log(`[reddit-devvit] subreddit=${subredditName} fetched_posts=${posts.length}`);
      for (const post of posts) {
        const text = [
          post.title,
          post.body,
          post.selftext,
          post.url,
          permalinkForPost(post),
        ].map(normalizeText).join('\n');
        for (const item of watchlist) {
          const matchedTerms = matchesItem(text, item);
          if (!matchedTerms.length) continue;
          signals.push(signalForPost(post, subredditName, item, matchedTerms));
        }
      }
    } catch (error) {
      console.log(`[reddit-devvit] subreddit=${subredditName} error=${error instanceof Error ? error.message : String(error)}`);
    }
  }

  await postSignals(baseUrl, sharedToken, signals);
  console.log(`[reddit-devvit] collect done signals=${signals.length}`);
  return signals.length;
}

async function ensureRecurringCollector(context: any): Promise<void> {
  const jobs = await context.scheduler.listJobs();
  const hasHourlyJob = jobs.some((job: any) => job?.name === COLLECT_JOB_NAME && 'cron' in job && job.cron === DEFAULT_COLLECT_CRON);
  if (!hasHourlyJob) {
    await context.scheduler.runJob({
      name: COLLECT_JOB_NAME,
      cron: DEFAULT_COLLECT_CRON,
      data: { source: 'app_install' },
    });
    console.log(`[reddit-devvit] scheduled cron=${DEFAULT_COLLECT_CRON}`);
  }
}

async function bootstrapCollector(context: any, reason: string): Promise<void> {
  try {
    await ensureRecurringCollector(context);
    const count = await collectRedditSignals(context);
    console.log(`[reddit-devvit] bootstrap reason=${reason} signals=${count}`);
  } catch (error) {
    console.log(`[reddit-devvit] bootstrap reason=${reason} error=${error instanceof Error ? error.message : String(error)}`);
  }
}

Devvit.addSchedulerJob({
  name: COLLECT_JOB_NAME,
  onRun: async (_event, context) => {
    await collectRedditSignals(context);
  },
});

Devvit.addTrigger({
  event: 'AppInstall',
  onEvent: async (_event, context) => {
    await bootstrapCollector(context, 'AppInstall');
  },
});

Devvit.addTrigger({
  event: 'AppUpgrade',
  onEvent: async (_event, context) => {
    await bootstrapCollector(context, 'AppUpgrade');
  },
});

Devvit.addMenuItem({
  label: 'AI Radar: run Reddit collector now',
  location: 'subreddit',
  onPress: async (_event, context) => {
    const count = await collectRedditSignals(context);
    context.ui.showToast(`AI Radar Reddit collector sent ${count} signals.`);
  },
});

export default Devvit;
