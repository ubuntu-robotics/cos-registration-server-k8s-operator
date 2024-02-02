# COS registration server Operator (k8s)

The [COS registration server](https://github.com/ubuntu-robotics/cos-registration-server) but as an operator

## Basic Deployment

The charm is still under development and is not available yet on CharmHub.

The deployment assumes that a Juju model is deployed with microk8s. Instructions on how to set up microk8s with Juju are available [here](https://juju.is/docs/sdk/set-up-your-development-environment#heading--install-microk8s).

To deploy the local charm follow these instructions:

- Clone the source repository

  ```
  git clone https://github.com/ubuntu-robotics/cos-registration-server-k8s-operator.git
  ```

- Build the charm with

  ```
  charmcraft pack
  ```

- Deploy the charm with the following command:

  ```
  juju deploy ./cos-registration-server_ubuntu-22.04-amd64.charm --resource TBD
  ```

- Test the installation by executing the following command:

  ```
  curl -v <unit_ip>:80/api/v1/devices
  ```



## COS lite deployment

This charm can be integrated with the [COS lite bundle](https://github.com/canonical/cos-lite-bundle)

An overlay is offered in this repository to ease up deployment.

To deploy with COS lite bundle follow these instructions:

- Clone the source repository

  ```
  git clone https://github.com/ubuntu-robotics/cos-registration-server-k8s-operator.git
  ```

- Enter the folder

  ```
  cd cos-registration-server-k8s-operator
  ```

- Build the charm with

  ```
  charmcraft pack
  ```

- Deploy cos-lite bundle with the robotics overlay as follows:

  ```
  juju deploy cos-lite --trust --overlay ./robotics-overlay.yaml
  ```
  NB. this bundle is in development and attempts to deploy additional and local charms. Before deploying make sure to have these charms in the folder.

  Once deployed the charm will be accessible via traefik at the following link:

  ```
  http://traefik-virtual-ip/<juju-model-name>-cos-registration-server/
  ```
