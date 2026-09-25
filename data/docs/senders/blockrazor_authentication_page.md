> For the complete documentation index, see [llms.txt](https://docs.blockrazor.io/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.blockrazor.io/get-started/authentication.md).

# Get a BlockRazor Auth Token | API Authentication

Learn how to create a BlockRazor account, obtain an Auth Token, and add the auth value to supported API requests before integrating BlockRazor services.

{% stepper %}
{% step %}
**Create Account**

[Sign up](https://blockrazor.io/#/register?redirect=onboarding) to create an account
{% endstep %}

{% step %}
**Log in**

<figure><img src="https://2581477772-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2FjbyfG8gOgcdsK3wVxNdQ%2Fuploads%2Fo9R9Dl5ai192AeTH0gMk%2Fimage.png?alt=media&amp;token=b6f3d0dd-d69d-4201-91a7-56a44e8a6193" alt=""><figcaption></figcaption></figure>

Log in and obtain the auth token in the home page
{% endstep %}
{% endstepper %}


---

# Agent Instructions
This documentation is published with GitBook. GitBook is the documentation platform designed so that both humans and AI agents can read, navigate, and reason over technical content effectively. Learn more at gitbook.com.

## Querying This Documentation
If you need additional information that is not directly available in this page, you can query the documentation dynamically by asking a question.

Perform an HTTP GET request on the current page URL with the `ask` query parameter, and the optional `goal` query parameter:

```
GET https://docs.blockrazor.io/get-started/authentication.md?ask=<question>&goal=<endgoal>
```

`ask` is the immediate question: it should be specific, self-contained, and written in natural language.
`goal` is optional and describes the broader end goal you are ultimately trying to accomplish on behalf of the user. GitBook uses it to tailor the answer towards what is most useful for that goal.

The response will contain a direct answer to the question and relevant excerpts and sources from the documentation.

Use this mechanism when the answer is not explicitly present in the current page, you need clarification or additional context, or you want to retrieve related documentation sections.
