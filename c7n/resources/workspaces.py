# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0
import functools
import itertools

from c7n.filters import ValueFilter
from c7n.filters.kms import KmsRelatedFilter
from c7n.manager import resources
from c7n.query import QueryResourceManager, TypeInfo
from c7n.tags import universal_augment
from c7n.utils import local_session, type_schema, chunks
from c7n.filters.iamaccess import CrossAccountAccessFilter
from c7n.resolver import ValuesFrom


@resources.register('workspaces')
class Workspace(QueryResourceManager):

    class resource_type(TypeInfo):
        service = 'workspaces'
        enum_spec = ('describe_workspaces', 'Workspaces', None)
        arn_type = 'workspace'
        name = id = dimension = 'WorkspaceId'
        universal_taggable = True
        cfn_type = 'AWS::WorkSpaces::Workspace'

    def augment(self, resources):
        return universal_augment(self, resources)


@Workspace.filter_registry.register('connection-status')
class WorkspaceConnectionStatusFilter(ValueFilter):
    """Filter Workspaces based on user connection information

    :example:

    .. code-block:: yaml

            policies:

              - name: workspaces-abandoned
                resource: workspaces
                filters:
                  - type: connection-status
                    value_type: age
                    key: LastKnownUserConnectionTimestamp
                    op: ge
                    value: 90

              - name: workspaces-expensive-zombies
                resource: workspaces
                filters:
                  - "WorkspaceProperties.RunningMode": ALWAYS_ON
                  - type: connection-status
                    value_type: age
                    key: LastKnownUserConnectionTimestamp
                    op: ge
                    value: 30
    """

    schema = type_schema('connection-status', rinherit=ValueFilter.schema)
    schema_alias = False
    permissions = ('workspaces:DescribeWorkspacesConnectionStatus',)
    annotation_key = 'c7n:ConnectionStatus'

    def get_connection_status(self, client, workspace_ids):
        connection_status_chunk = self.manager.retry(
            client.describe_workspaces_connection_status,
            WorkspaceIds=workspace_ids
        )['WorkspacesConnectionStatus']

        return connection_status_chunk

    def process(self, resources, event=None):
        client = local_session(self.manager.session_factory).client('workspaces')
        annotate_map = {r['WorkspaceId']: r for r in resources if self.annotation_key not in r}
        with self.executor_factory(max_workers=2) as w:
            self.log.debug(
                'Querying connection status for %d workspaces' % len(annotate_map))
            for status in itertools.chain(*w.map(
                functools.partial(self.get_connection_status, client),
                chunks(annotate_map.keys(), 25)
            )):
                annotate_map[status['WorkspaceId']][self.annotation_key] = status
        return list(filter(self, resources))

    def get_resource_value(self, k, i):
        return super(WorkspaceConnectionStatusFilter, self).get_resource_value(
            k, i[self.annotation_key])


@Workspace.filter_registry.register('kms-key')
class KmsFilter(KmsRelatedFilter):

    RelatedIdsExpression = 'VolumeEncryptionKey'


@resources.register('workspaces-image')
class WorkspaceImage(QueryResourceManager):

    class resource_type(TypeInfo):
        service = 'workspaces'
        enum_spec = ('describe_workspace_images', 'Images', None)
        arn_type = 'workspaceimage'
        name = id = 'ImageId'
        universal_taggable = True

    augment = universal_augment


@WorkspaceImage.filter_registry.register('cross-account')
class WorkspaceImageCrossAccount(CrossAccountAccessFilter):

    schema = type_schema(
        'cross-account',
        # white list accounts
        whitelist_from=ValuesFrom.schema,
        whitelist={'type': 'array', 'items': {'type': 'string'}})

    permissions = ('workspaces:DescribeWorkspaceImagePermissions',)

    def process(self, resources, event=None):
        client = local_session(self.manager.session_factory).client('workspaces')
        allowed_accounts = set(self.get_accounts())
        results = []
        for r in resources:
            found = False
            try:
                accts = client.describe_workspace_image_permissions(
                    ImageId=r['ImageId']).get('ImagePermissions')
                for a in accts:
                    account_id = a['SharedAccountId']
                    if (account_id not in allowed_accounts):
                        r.setdefault('c7n:CrossAccountViolations', []).append(account_id)
                        found = True
                if found:
                    results.append(r)
            except client.exceptions.ResourceNotFoundException:
                continue

        return results
