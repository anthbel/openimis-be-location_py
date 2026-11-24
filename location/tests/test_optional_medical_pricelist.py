import importlib.util
import os
import types
import unittest
from unittest import mock

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.test_helpers import create_imis_admin_role, create_test_interactive_user
from location.gql_mutations import (
    CreateHealthFacilityMutation,
    UpdateHealthFacilityMutation,
)
from location.models import HealthFacility
from location.services import HealthFacilityService
from location.test_helpers import (
    create_test_health_facility,
    create_test_health_facility_legal_form,
    create_test_location,
)

PRICELIST_APP = "medical_pricelist"
PRICELIST_DEPENDENCY = (
    PRICELIST_APP,
    "0002_itemspricelistmutation_servicespricelistmutation",
)
PRICELIST_FIELDS = {"services_pricelist", "items_pricelist"}
PRICELIST_REFUSAL = "medical_pricelist module is not installed"
MIGRATION_0011 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations",
    "0011_auto_20230317_0924.py",
)


def load_migration_0011():
    """Executes migration 0011 into a fresh module object, outside sys.modules."""
    spec = importlib.util.spec_from_file_location("location_migration_0011_copy", MIGRATION_0011)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Migration


def without_medical_pricelist():
    """Patches the app registry so that medical_pricelist reads as not installed."""
    is_installed = apps.is_installed
    get_model = apps.get_model

    def fake_is_installed(app_name):
        return False if app_name == PRICELIST_APP else is_installed(app_name)

    def fake_get_model(app_label, model_name=None, require_ready=True):
        label = app_label.split(".")[0]
        if label == PRICELIST_APP:
            raise LookupError("No installed app with label '%s'." % PRICELIST_APP)
        return get_model(app_label, model_name, require_ready=require_ready)

    return mock.patch.multiple(apps, is_installed=fake_is_installed, get_model=fake_get_model)


def add_field_names(migration):
    return [op.name for op in migration.operations if type(op).__name__ == "AddField"]


def health_facility_data(code, location, legal_form, **extra):
    return {
        "code": code,
        "name": "Test HF " + code,
        "legal_form_id": legal_form.code,
        "level": "H",
        "location_id": location.id,
        "care_type": "B",
        **extra,
    }


class HealthFacilityPricelistFieldsTest(TestCase):
    def test_pricelist_fields_exist_only_with_medical_pricelist(self):
        field_names = {f.name for f in HealthFacility._meta.get_fields()}
        self.assertEqual(
            PRICELIST_FIELDS <= field_names, apps.is_installed(PRICELIST_APP)
        )
        self.assertEqual(
            bool(PRICELIST_FIELDS & field_names), apps.is_installed(PRICELIST_APP)
        )


class MedicalPricelistAbsentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(
            username="tstHfNoPricelist", roles=[create_imis_admin_role().id]
        )
        cls.district = create_test_location("D")
        cls.legal_form = create_test_health_facility_legal_form()
        cls.hf = create_test_health_facility("NOPLHF", cls.district.id)

    def test_migration_0011_has_no_pricelist_dependency_or_fields(self):
        with without_medical_pricelist():
            migration = load_migration_0011()
        self.assertNotIn(PRICELIST_DEPENDENCY, migration.dependencies)
        self.assertEqual(
            migration.dependencies,
            [("location", "0010_insert_create_region_location_perms")],
        )
        self.assertEqual(
            add_field_names(migration),
            ["legal_form", "location", "sub_level", "uuid", "uuid"],
        )

    def test_create_mutation_refuses_services_pricelist_id(self):
        data = health_facility_data(
            "NOPLC1", self.district, self.legal_form, services_pricelist_id=1
        )
        with without_medical_pricelist():
            errors = CreateHealthFacilityMutation.async_mutate(self.user, **data)
        self.assertIsNotNone(errors)
        self.assertIn(PRICELIST_REFUSAL, errors[0]["detail"])
        self.assertFalse(HealthFacility.objects.filter(code="NOPLC1").exists())

    def test_update_mutation_refuses_items_pricelist_id(self):
        data = health_facility_data(
            self.hf.code,
            self.district,
            self.legal_form,
            uuid=self.hf.uuid,
            name="Renamed NOPLHF",
            items_pricelist_id=1,
        )
        with without_medical_pricelist():
            errors = UpdateHealthFacilityMutation.async_mutate(self.user, **data)
        self.assertIsNotNone(errors)
        self.assertIn(PRICELIST_REFUSAL, errors[0]["detail"])
        self.hf.refresh_from_db()
        self.assertNotEqual(self.hf.name, "Renamed NOPLHF")

    def test_service_refuses_pricelist_id(self):
        data = health_facility_data(
            "NOPLC2", self.district, self.legal_form, items_pricelist_id=1
        )
        data.update(audit_user_id=-1, validity_from="2019-01-01")
        with without_medical_pricelist():
            with self.assertRaisesMessage(ValidationError, PRICELIST_REFUSAL):
                HealthFacilityService(self.user).update_or_create(data)

    def test_create_mutation_ignores_null_pricelist_ids(self):
        data = health_facility_data(
            "NOPLC3",
            self.district,
            self.legal_form,
            services_pricelist_id=None,
            items_pricelist_id=None,
        )
        with without_medical_pricelist():
            errors = CreateHealthFacilityMutation.async_mutate(self.user, **data)
        self.assertIsNone(errors)
        self.assertTrue(HealthFacility.objects.filter(code="NOPLC3").exists())

    def test_reset_before_update_leaves_pricelist_attributes_alone(self):
        hf = types.SimpleNamespace()
        with without_medical_pricelist():
            HealthFacilityService._reset_health_facility_before_update(hf)
        self.assertFalse(hasattr(hf, "services_pricelist"))
        self.assertFalse(hasattr(hf, "items_pricelist"))
        self.assertIsNone(hf.care_type)


@unittest.skipUnless(apps.is_installed(PRICELIST_APP), "medical_pricelist is not installed")
class MedicalPricelistInstalledTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        from medical_pricelist.test_helpers import (
            create_test_item_pricelist,
            create_test_service_pricelist,
        )

        cls.user = create_test_interactive_user(
            username="tstHfPricelist", roles=[create_imis_admin_role().id]
        )
        cls.district = create_test_location("D")
        cls.legal_form = create_test_health_facility_legal_form()
        cls.services_pricelist = create_test_service_pricelist(cls.district.id)
        cls.items_pricelist = create_test_item_pricelist(cls.district.id)
        cls.hf = create_test_health_facility(
            "PLHF",
            cls.district.id,
            custom_props={
                "services_pricelist": cls.services_pricelist,
                "items_pricelist": cls.items_pricelist,
            },
        )

    def test_migration_0011_keeps_pricelist_dependency_and_fields(self):
        migration = load_migration_0011()
        self.assertEqual(
            migration.dependencies,
            [("location", "0010_insert_create_region_location_perms"), PRICELIST_DEPENDENCY],
        )
        self.assertEqual(
            add_field_names(migration),
            [
                "items_pricelist",
                "legal_form",
                "location",
                "services_pricelist",
                "sub_level",
                "uuid",
                "uuid",
            ],
        )

    def test_create_mutation_sets_pricelists(self):
        data = health_facility_data(
            "PLC1",
            self.district,
            self.legal_form,
            services_pricelist_id=self.services_pricelist.id,
            items_pricelist_id=self.items_pricelist.id,
        )
        errors = CreateHealthFacilityMutation.async_mutate(self.user, **data)
        self.assertIsNone(errors)
        hf = HealthFacility.objects.get(code="PLC1", validity_to__isnull=True)
        self.assertEqual(hf.services_pricelist_id, self.services_pricelist.id)
        self.assertEqual(hf.items_pricelist_id, self.items_pricelist.id)

    def test_update_without_pricelist_ids_clears_pricelists(self):
        self.assertEqual(self.hf.services_pricelist_id, self.services_pricelist.id)
        data = health_facility_data(
            self.hf.code, self.district, self.legal_form, uuid=self.hf.uuid
        )
        errors = UpdateHealthFacilityMutation.async_mutate(self.user, **data)
        self.assertIsNone(errors)
        self.hf.refresh_from_db()
        self.assertIsNone(self.hf.services_pricelist_id)
        self.assertIsNone(self.hf.items_pricelist_id)

    def test_absent_mode_update_keeps_existing_pricelists(self):
        data = health_facility_data(
            self.hf.code, self.district, self.legal_form, uuid=self.hf.uuid
        )
        with without_medical_pricelist():
            errors = UpdateHealthFacilityMutation.async_mutate(self.user, **data)
        self.assertIsNone(errors)
        self.hf.refresh_from_db()
        self.assertEqual(self.hf.services_pricelist_id, self.services_pricelist.id)
        self.assertEqual(self.hf.items_pricelist_id, self.items_pricelist.id)
