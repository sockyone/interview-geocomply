# Supported application charts

We maintain and publish these Helm charts independently of users' applications.
An [ApplicationDefinition](../../ApplicationDefinitions/README.md) selects a
chart/version from this catalog and supplies values for the user's image.

| Chart | Version | Supported application shape |
| --- | --- | --- |
| [http-service](http-service/README.md) | `0.1.0` | Single HTTP container, Deployment, ClusterIP Service, optional HPA |

Add other application shapes as separate supported charts with their own values
schemas. Application-specific image references, commands, probe paths, and
dependency settings belong in AD values. Application source may live anywhere.
