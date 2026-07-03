/**
 * Public surface of the MCP-servers feature slice (#228, ADR-0012). Routes and
 * other features import from here, never from deep paths (frontend/AGENTS.md: no
 * cross-feature deep imports).
 */
export { ServersPage } from './components/ServersPage';
export { ServersPanel } from './components/ServersPanel';
export { ServerCard } from './components/ServerCard';
export { RegisterServerModal } from './components/RegisterServerModal';
export { ServerDetailDrawer } from './components/ServerDetailDrawer';
export {
  useMcpServers,
  useMcpServer,
  useMcpServerTools,
  useRegisterMcpServer,
  useUpdateMcpServer,
  useTestMcpServer,
  useDeleteMcpServer,
} from './model/queries';
