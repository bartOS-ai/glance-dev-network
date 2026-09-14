# Facebook Page Followers

A live follower counter for one public Facebook Page: the exact count, the
page's name with its verified badge, and the handle. No API key or login.

## Settings

| setting | what it is |
|---|---|
| **Facebook Page** | The name from the page's URL. For `https://www.facebook.com/nasa` enter `nasa`. A numeric page ID (`54971236771`) works too. Pasting the whole URL also works; the app strips the site part. |

## Notes

- The count comes from Facebook's own Page Plugin, the embeddable box any
  website can show, which is why nothing has to be signed up for. It works
  for public **Pages** only. Personal profiles and unpublished pages show
  `NOT FOUND`.
- **Check the name on the panel.** If Facebook does not know the name you
  typed, it may serve the closest page it can find instead of an error.
  The panel always shows the page's real name next to the count so a wrong
  page is obvious at a glance.
- Refresh is every 10 minutes.
