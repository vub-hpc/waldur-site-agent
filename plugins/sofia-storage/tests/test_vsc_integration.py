"""Test VSC integration with waldur-vub-storage-plugin backend."""

import unittest
from unittest.mock import Mock, patch, MagicMock
from waldur_api_client.models.resource import Resource as WaldurResource
from gpfs.backend import VubGpfsBackend
from gpfs.vsc import VscBackend

class TestVscIntegration(unittest.TestCase):
    """Test VSC integration functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.backend_settings = {
            "storage_path": "/gpfs/storage1",
            "vsc_token": "test-token-123",
            "vsc_group_prefix": "proj_"
        }

        self.backend_components = {
            "storage": {
                "measured_unit": "GB",
                "accounting_type": "limit",
                "label": "Storage",
                "unit_factor": 1024
            }
        }

        # Create mock VSC client
        self.mock_vsc_client = Mock()
        self.mock_vsc_client.create_or_update_group.return_value = {"success": True}

        # Patch VscBackend to use our mock client
        self.patch_vsc = patch.object(VscBackend, '__init__', return_value=None)
        self.patch_vsc.start()
        self.vsc_backend = VscBackend("test-token")
        self.vsc_backend.client = self.mock_vsc_client

        # Create backend instance
        self.backend = VubGpfsBackend(self.backend_settings, self.backend_components)
        self.backend.vsc_client = self.vsc_backend

    def tearDown(self):
        """Clean up test fixtures."""
        self.patch_vsc.stop()

    def test_vsc_client_initialization(self):
        """Test VSC client initialization in backend."""
        backend = VubGpfsBackend(self.backend_settings, self.backend_components)
        self.assertIsNotNone(backend.vsc_client)
        self.assertEqual(backend.vsc_group_prefix, "proj_")

    def test_vsc_client_without_token(self):
        """Test backend without VSC token."""
        settings_no_token = {"storage_path": "/gpfs/storage1"}
        backend = VubGpfsBackend(settings_no_token, self.backend_components)
        self.assertIsNone(backend.vsc_client)

    def test_project_group_creation(self):
        """Test VSC project group creation."""
        # Mock waldur resource
        waldur_resource = Mock(spec=WaldurResource)
        waldur_resource.name = "Test Project"
        waldur_resource.slug = "test-project"
        waldur_resource.backend_id = "test-project"

        # Mock limits
        limits = Mock()
        limits.to_dict.return_value = {"storage": 100}
        waldur_resource.limits = limits

        # Test VSC group creation
        self.vsc_backend.make_project_group("test-project", "Test Project")

        # Verify the correct call was made
        self.mock_vsc_client.create_or_update_group.assert_called_with(
            "proj_test-project",
            description="Storage group for project Test Project"
        )

    def test_resource_creation_creates_vsc_group(self):
        """Test that resource creation triggers VSC group creation."""
        # Mock waldur resource
        waldur_resource = Mock(spec=WaldurResource)
        waldur_resource.name = "Data Project"
        waldur_resource.slug = "data-project"
        waldur_resource.backend_id = "data-project"

        # Mock limits
        limits = Mock()
        limits.to_dict.return_value = {"storage": 200}
        waldur_resource.limits = limits

        # Mock GPFS client methods
        with patch.object(self.backend.client, 'create_fileset'), \
             patch.object(self.backend.client, 'set_quota'), \
             patch.object(self.backend.client, 'get_repquota', return_value=[]):

            # Create resource
            result = self.backend.create_resource(waldur_resource)

            # Verify VSC group was created
            self.mock_vsc_client.create_or_update_group.assert_called_with(
                "proj_data-project",
                description="Storage group for project Data Project"
            )

            # Verify result
            self.assertEqual(result.backend_id, "data-project")

    def test_resource_creation_fails_without_vsc(self):
        """Test that resource creation fails without VSC integration."""
        # Create backend without VSC client
        settings_no_vsc = {"storage_path": "/gpfs/storage1"}
        backend = VubGpfsBackend(settings_no_vsc, self.backend_components)

        # Mock waldur resource
        waldur_resource = Mock(spec=WaldurResource)
        waldur_resource.name = "Test Project"
        waldur_resource.slug = "test-project"

        # Mock limits
        limits = Mock()
        limits.to_dict.return_value = {"storage": 100}
        waldur_resource.limits = limits

        # Should raise exception
        with self.assertRaises(Exception) as context:
            backend.create_resource(waldur_resource)

        self.assertIn("Cannot create storage resource without VSC account page integration", str(context.exception))

    def test_vsc_group_prefix_customization(self):
        """Test custom VSC group prefix."""
        custom_settings = {
            "storage_path": "/gpfs/storage1",
            "vsc_token": "test-token",
            "vsc_group_prefix": "storage_"
        }

        backend = VubGpfsBackend(custom_settings, self.backend_components)
        self.assertEqual(backend.vsc_group_prefix, "storage_")

    def test_project_group_name_generation(self):
        """Test project group name generation."""
        vsc_backend = VscBackend("test-token")
        group_name = vsc_backend.project_group_name("my-project")
        self.assertEqual(group_name, "proj_my-project")

    def test_get_project_group(self):
        """Test getting project group information."""
        mock_group_info = {"name": "proj_test-group", "description": "Test group"}

        # Mock the client's get_group method
        self.mock_vsc_client.get_group.return_value = mock_group_info

        result = self.vsc_backend.get_project_group("test-group")
        self.assertEqual(result, mock_group_info)
        self.mock_vsc_client.get_group.assert_called_with("proj_test-group")

if __name__ == "__main__":
    unittest.main()
