import jinja2
import subprocess
from http import HTTPStatus
import os
import yaml
import jinja2.exceptions
from bottle import route, run, response
from kubernetes import client as kubernetes_client, config as k8s_config


# I load the .KUBE/CONFIG file for switching to different contexts

def load_k8s_config(deployment_env, project_name=None):
    env_map = { 'master': {'default': 'KUBE_CONFIG_MASTER'}}
    config_file_path = env_map[deployment_env].get(project_name) or env_map[deployment_env]['default']
    k8s_config.load_kube_config(os.environ[config_file_path])


# SNIPPETS
def _load_yaml_file(deployment_env, project_name, app_name):
    file_path = _make_deployment_manifest_path(deployment_env, project_name, app_name)

    with open(file_path) as f:
        yaml_file = yaml.load_all(f.read(), Loader=yaml.Loader)

    for manifest in yaml_file:
        if manifest['kind'] == 'Deployment':
            return manifest

    raise Exception('no deployment found')


def _get_template_name_and_namespace(deployment_manifest):
    return deployment_manifest['metadata']['name'], deployment_manifest['metadata']['namespace']


def _make_deployment_manifest_path(deployment_env, project_name, app_name):
    return "./templates/{}/{}/{}.yaml".format(deployment_env, project_name, app_name)



# ROUTES (I use this for change the docker image version in our deployment.yaml file)

@route('/<deployment_env>/<project_name>/<app_name>/<image_tag>')
def apply_deployment(deployment_env, project_name, app_name, image_tag):
    file_path = "{}/{}/{}.yaml".format(deployment_env, project_name, app_name)

    templateLoader = jinja2.FileSystemLoader(searchpath="./templates")
    templateEnv = jinja2.Environment(loader=templateLoader)

    try:
        template = templateEnv.get_template(file_path)
    except jinja2.exceptions.TemplateNotFound as exc:
        response.status = HTTPStatus.UNPROCESSABLE_ENTITY
        return {'error': 'template `{}` not found'.format(exc.message)}

    rendered_template = template.render(image_tag=image_tag)

    with open("./latest/{}".format(file_path), "w") as f:
        f.write(rendered_template)

    kube_config = os.environ["KUBE_CONFIG_{}".format(deployment_env.upper())]

    result = subprocess.run(["/usr/local/bin/kubectl", "--kubeconfig", kube_config, "apply", "-f", "./latest/{}".format(file_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if result.returncode or result.stderr:
        response.status = HTTPStatus.UNPROCESSABLE_ENTITY
        return {'error': result.stderr.decode()}

    return {'output': result.stdout.decode()}


# ROUTES (I use this for get the latest status of my deployment)

@route('/rollouts/<deployment_env>/<project_name>/<app_name>')
def get_rollout_status(deployment_env, project_name, app_name):

    deployment_manifest = _load_yaml_file(deployment_env, project_name, app_name)

    load_k8s_config(deployment_env)

    api = kubernetes_client.ExtensionsV1beta1Api()

    deployment_name, deployment_namespace = _get_template_name_and_namespace(deployment_manifest)

    try:
        deployment_obj = api.read_namespaced_deployment_status(deployment_name, deployment_namespace)
    except kubernetes_client.rest.ApiException as exc:
        if exc.status == 404:
            return {'error': 'deployment "{}" not found'.format(deployment_name)}
        raise Exception('unkown exception!')

    deployment_replicas = deployment_obj.spec.replicas
    deployment_status = deployment_obj.status

    for condition in deployment_status.conditions:
        if condition.type == 'Progressing' and condition.status == 'False':
            return {'status': 'failed', 'error': condition.reason,
                    'available_replicas': deployment_status.available_replicas or 0}

    if deployment_status.updated_replicas == deployment_replicas and \
            deployment_status.replicas == deployment_replicas and \
            deployment_status.available_replicas == deployment_replicas and \
            deployment_status.observed_generation >= deployment_obj.metadata.generation:
        return {'status': 'completed', 'deployment': deployment_name,
                'available_replicas': deployment_status.available_replicas or 0}

    return {'status': 'progressing', 'replicas': deployment_replicas,
            'updated_replicas': deployment_status.updated_replicas,
            'available_replicas': deployment_status.available_replicas or 0}

run(host='0.0.0.0', port=8080, DEBUG=True)
