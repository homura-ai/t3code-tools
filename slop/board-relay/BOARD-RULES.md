# Board rules for agents (template)

Paste this into the project's agent instructions and fill in the ids. The relay
expects card titles in the form `<agent name>: <matter>`.

- The board is how agents reach the humans: a Wekan board at `BOARD_URL`, with
  lists such as **Human** (any human can take it), one list per person, and **Done**.
- Post a card only for an important result, a blocker, or a decision only a human
  can make. Never post one for something you can resolve yourself, and never wait
  on an answer. Carry on with your best judgement.
- Card title: your agent name, a colon, then the result or the exact decision
  needed, for example "Builder: switch to plan B?". Put the evidence and your
  recommendation in the description.
- One card per matter. If it changes, comment on your existing card.
- A human answers by commenting on the card, and the comment arrives in your
  thread as a message with the card's id. Act on it, comment back if needed,
  and move the card yourself: to Done once it's settled.
- API: `http://127.0.0.1:8090/api` with no Authorization header (the local proxy
  adds the agents token). Agents user `AGENTS_USER_ID`, board `BOARD_ID`,
  swimlane `SWIMLANE_ID`, lists `LIST_IDS`.
  - Add a card: POST `/api/boards/<board>/lists/<list>/cards` with `authorId`,
    `title`, `description` and `swimlaneId`.
  - Comment: POST `/api/boards/<board>/cards/<card>/comments` with `authorId` and
    `comment`. Read comments with a GET on the same address.
  - List a list's cards: GET `/api/boards/<board>/lists/<list>/cards`.
  - Move a card: PUT `/api/boards/<board>/lists/<current list>/cards/<card>` with
    `listId` set to the new list.
- If the board is down, say so in your own thread. Don't block on it.
