-- 0002: one example business for staging and demonstrations. Neutral placeholder data only.
--
-- Safe to run more than once: rows are matched by their slugs and left alone if they already exist.
-- Prices are in minor units (pence). Weekday: 0 = Sunday ... 6 = Saturday.

INSERT INTO businesses (slug, name, time_zone, currency, break_minutes, notice_minutes, window_days)
VALUES ('example', 'Example Advisory', 'Europe/London', 'GBP', 0, 240, 30)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO services (business_id, slug, name, duration_minutes, price_minor, sort_order)
SELECT b.id, s.slug, s.name, s.duration_minutes, s.price_minor, s.sort_order
FROM businesses b
CROSS JOIN (
    VALUES
        ('intro', 'Introductory call', 30, 0, 1),
        ('session', 'Advisory session', 60, 45000, 2),
        ('review', 'Business review', 90, 90000, 3)
) AS s (slug, name, duration_minutes, price_minor, sort_order)
WHERE b.slug = 'example'
ON CONFLICT (business_id, slug) DO NOTHING;

INSERT INTO weekly_hours (business_id, weekday, start_time, end_time)
SELECT b.id, h.weekday, h.start_time, h.end_time
FROM businesses b
CROSS JOIN (
    VALUES
        (1, time '09:00', time '17:00'),
        (2, time '09:00', time '17:00'),
        (3, time '09:00', time '17:00'),
        (4, time '09:00', time '17:00'),
        (5, time '09:00', time '17:00'),
        (6, time '09:00', time '13:00')
) AS h (weekday, start_time, end_time)
WHERE b.slug = 'example'
  AND NOT EXISTS (SELECT 1 FROM weekly_hours w WHERE w.business_id = b.id);
