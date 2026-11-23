CREATE TABLE IF NOT EXISTS warehouse_pick_line_swaps (
    id INT AUTO_INCREMENT PRIMARY KEY,
    picklist_id INT NOT NULL,
    recalled_lot_id INT NOT NULL,
    recalled_units INT UNSIGNED NOT NULL,
    replacement_lines JSON NOT NULL,
    swapped_by INT NOT NULL,
    swapped_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_swap_pick_lot (picklist_id, recalled_lot_id),
    FOREIGN KEY (picklist_id) REFERENCES warehouse_picklists(id),
    FOREIGN KEY (recalled_lot_id) REFERENCES warehouse_lots(id),
    FOREIGN KEY (swapped_by) REFERENCES users(id)
);
