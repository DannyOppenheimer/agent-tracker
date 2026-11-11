"""Offline title extraction from real-world prompt shapes."""
import unittest

from titles import heuristic_title, request_title


class TitleTests(unittest.TestCase):
    def test_skips_context_for_the_actual_request(self):
        self.assertEqual(request_title('This is an asset pipeline to unity. Currently, our solar system is built. '
                                       'Build the scenes for the other solar systems.'),
                         'Build the scenes for the other solar systems')
        self.assertEqual(request_title('I renamed this folder to Shutternaut. Does anything in the docs need to change?'),
                         'Does anything in the docs need to change')

    def test_strips_greetings_and_politeness(self):
        self.assertEqual(request_title('hey, ok so can you please add a settings panel?'), 'Add a settings panel')
        self.assertEqual(request_title("I think we need to revisit the player arms model."), 'Revisit the player arms model')
        self.assertEqual(request_title('Your job is only to tune the head sway.'), 'Tune the head sway')

    def test_removes_code_urls_and_long_paths(self):
        title = request_title('Fix the crash in C:\\Users\\me\\project\\src\\camera.py, see '
                              'https://github.com/org/repo/issues/12\n```py\nraise Error\n```')
        self.assertEqual(title, 'Fix the crash in camera.py, see github.com')

    def test_trims_at_word_boundary_without_dangling_words(self):
        title = request_title('Reduce the rotation speed of the universe background seen through the window '
                              'inside the space station so it feels calmer', width=60)
        self.assertTrue(title.endswith('…'))
        self.assertLessEqual(len(title), 61)
        self.assertNotRegex(title, r'\b(the|of|and|to)…$')

    def test_chatter_falls_through_to_next_prompt(self):
        self.assertEqual(heuristic_title(['hi', 'thanks!', 'Fix the darkroom door']), 'Fix the darkroom door')

    def test_unparseable_prompt_still_gets_a_title(self):
        self.assertEqual(heuristic_title(['Test']), 'Test')
        self.assertEqual(heuristic_title([]), '')
        self.assertEqual(heuristic_title([None, '  ']), '')


if __name__ == '__main__':
    unittest.main()
