from unittest import TestCase
from . import views


class ModelAlignmentTests(TestCase):
    def test_checklist_matches_actual_model_diseases(self):
        from ultralytics import YOLO
        from django.conf import settings
        model = YOLO(str(settings.BASE_DIR / 'static' / 'models' / 'best.pt'))
        labels = tuple(model.names[index] for index in sorted(model.names))
        self.assertEqual(views.MODEL_CLASS_NAMES, labels)
        expected = [views.normalize_predicted_class(name) for name in labels
                    if views.normalize_predicted_class(name) != 'Healthy Eggplant Leaf']
        items = views.build_disease_guide_items(None)
        self.assertEqual([item['name'] for item in items], expected)
        for item in items:
            self.assertTrue(item['about'])
            self.assertTrue(item['medicine'])

    def test_healthy_model_label_uses_existing_healthy_behavior(self):
        self.assertEqual(views.normalize_predicted_class('Healthy Leaf'), 'Healthy Eggplant Leaf')
        self.assertNotIn('Healthy Leaf', views.DISEASE_GUIDE_ORDER)

    def test_small_leaf_detection_checks_off_guide_item(self):
        items = views.build_disease_guide_items({'detected_diseases': [
            {'name': 'Small Leaf Disease', 'count': 1}]})
        self.assertEqual([item['name'] for item in items if item['detected']], ['Small Leaf Disease'])
