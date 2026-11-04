# TestSuites

A TestSuite holds **your user journeys**: Python functions that call your service and check the
responses, plus optional test data. It contains no load settings (users, stages, thresholds);
those live in the LoadTestDefinition. One folder per suite: `TestSuites/<name>/`.

```
TestSuites/checkout/
  suite.yaml        # names the journeys
  scenarios.py      # the journey functions
  fixtures.json     # optional test data, available as context.fixture
```

```yaml
# suite.yaml
apiVersion: perf-runner/v1alpha1
kind: TestSuite
metadata:
  name: checkout                    # tests refer to this name
spec:
  fixtures: fixtures.json
  journeys:
    browse:
      callable: scenarios:browse    # module:function
      description: View one product page and check its price
```

```python
# scenarios.py
from suite_runtime import require, payload

def browse(context):
    prices = context.fixture['productPrices']
    item = payload(context.client.get('/products/coffee', name='/products/{id}'), 200)
    require(item['priceCents'] == prices['coffee'], 'Unexpected price')
```

- `context.client` is an HTTP client pointed at your service; `name=` groups URLs in the report.
- `payload(response, status)` checks the status code and returns the JSON body.
- `require(condition, message)` fails the journey. Any failed request or check fails the test.
- Journeys are plain synchronous functions taking one `context`. The platform supplies the
  runner image; you never build one.

## Publish

```bash
./loadtest publish suite TestSuites/checkout --version 0.3.0
```

The code is validated without running it, then published as `checkout:0.3.0` and
`checkout:latest`. More detail, including running a suite locally against a service:
[runner README](../platform/runner/README.md).
