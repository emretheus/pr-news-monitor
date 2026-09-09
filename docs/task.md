 Coding Challenge: PR News Monitor
Scenario
Your client is a PR department at a large company. They want a dashboard that monitors news
about:
1. 2. Their company — direct mentions, press coverage, incidents
Their competitors — what the competition is doing
Optional (bonus): A third feed for general industry news.
The company and competitors should be configurable (not hardcoded). For this challenge, use
any company/competitor pair you like — e.g. a data center company and its rivals, a cloud
provider and its peers, etc.
For the purpose of this challenge, decide on a company to develop it for (Apple, Coca-cola,
etc…)
🔧 Requirements
1. News Feeds
●
●
●
●
Fetch recent articles using the NewsData.io API (free plan). If you want to use other
source for the task – feel free to.
Display at least two feeds: one for the company, one for competitors.
Each article should show: title, annotation/description/summary, source, published
date.
The company/competitor configuration should be easy to change (config file, env vars, or
UI — your call).
2. Full-Text Enrichment
NewsData.io only returns metadata and snippets — not full article text.
●
●
Use a library like Newspaper4k to fetch full article text from the article URLs.
Use the full text to improve your analysis (feed classification, grouping, summaries —
see below).
●
Not every URL will yield clean text. Handle failures gracefully.



3. Additional Source: Data Centre Magazine
Add https://datacentremagazine.com/ as a supplementary news source alongside
NewsData.io.
●
●
You'll need to figure out how to discover and fetch articles from this site.
Note: this site has protections that may require a non-trivial approach to scrape.
4. Story Grouping
Multiple outlets often cover the same story. Implement:
●
●
●
Deduplication — detect when multiple articles cover the same event/topic.
Clustering — group related articles into stories/topics.
Cluster descriptions — generate a short description for each cluster using an LLM.
Optional bonus: Generate cluster descriptions that synthesize multiple
perspectives across sources (e.g.,
"Reuters frames this as a regulatory issue,
while TechCrunch focuses on the market impact").
5. LLM Analysis
Use an LLM (via OpenRouter — see keys below) for at least the cluster descriptions above,
plus one or more of:
●
●
●
●
Classify articles into the correct feed (company vs. competitor vs. industry)
Sentiment per article or per cluster
Extract key entities or topics
Generate a daily briefing summary
6. Deployment Readiness
●
●
Prepare the project for deployment: Dockerfile, docker-compose, environment
configuration, README with setup instructions.
You may also deploy it (e.g. to a free tier somewhere), but this is optional.
7. Visualization
Optional. Only add charts/widgets if you believe they make the product genuinely more useful
for a PR person. Don't add visualizations just for show.



●
●
You have roughly 4 hours.
Use whatever tools you're comfortable with (AI coding assistants included).
Don't over-engineer — focus on working software and smart decisions.
📦 Deliverables
1. 2. 3. 4. A running prototype (locally is fine, deployed is a bonus)
Clean, readable code with deployment config
A short write-up (README or separate doc) covering:
○
How you'd take this to production
○
What you'd improve with more time
○
Trade-offs you made
○
Scaling/reliability/UX considerations
○
Architecture or model choices you'd revisit
A 5-minute walkthrough of what you built and your decisions

