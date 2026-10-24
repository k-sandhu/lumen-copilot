# Assistants frontend

The library, editor and test/debug surfaces implement the frozen assistants
contract. Server state lives in the slice's TanStack queries; Start chat creates
an assistant-bound session, opens it in the shared chat store with the assistant's
knowledge modes, and navigates to the chat route. Failed starts remain on the
originating card with an actionable error.

## Loading boundaries (#531)

The editor imports `ModelPicker` and the library imports `useChatStore` from
`@/features/chat/controls`, the documented lightweight public chat entry point.
They do not reach into chat's internal folders or import the full chat barrel.
Model selection stays local to the editor; Start chat uses the same singleton
as the chat workspace. Chat/Markdown/preview modules remain available behind the
actual chat or preview loading path.

Measured production JavaScript static closure of Assistants (same build config,
shared chunks included, sum of each chunk's raw and gzip bytes):

|                                                   |    Before |   After |
| ------------------------------------------------- | --------: | ------: |
| Raw bytes                                         | 1,284,414 | 792,269 |
| Gzip bytes                                        |   346,762 | 211,901 |
| Heavy Markdown/ChatView modules in static closure |       285 |       0 |

This removes 492,145 raw bytes and 134,861 gzip bytes from the route's static
JavaScript dependencies; these are build sizes, not a measured network timing.
`route-loading.test.ts` builds production output and walks transitive static
imports. It also requires picker/store modules and deferred chat/Markdown/preview
output so passing cannot mean deleting functionality. The production-browser
regression checks that library/editor navigation requests no Markdown script,
model choice stays local, and Start chat then fetches the renderer and opens the
assistant session with its original model.
