-- Migration for feedback table
CREATE TABLE `feedback` (
    `id` int(11) NOT NULL AUTO_INCREMENT,
    `user_id` int(11) NOT NULL,
    `municipality_id` int(11) NOT NULL,
    `rating` int(11) NOT NULL CHECK (
        rating >= 1
        AND rating <= 5
    ),
    `message` text NOT NULL,
    `is_anonymous` tinyint(1) DEFAULT 0,
    `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
    PRIMARY KEY (`id`),
    KEY `feedback_user_id_foreign` (`user_id`),
    KEY `feedback_municipality_id_foreign` (`municipality_id`),
    CONSTRAINT `feedback_user_id_foreign` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
    CONSTRAINT `feedback_municipality_id_foreign` FOREIGN KEY (`municipality_id`) REFERENCES `municipalities` (`id`) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci;
-- Indexing
CREATE INDEX `idx_feedback_municipality_created` ON `feedback` (`municipality_id`, `created_at` DESC);